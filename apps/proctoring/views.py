import json
import uuid

from django.conf import settings
from django.core.files.base import ContentFile
from django.db.models import Count, Prefetch, Q
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST
from django_ratelimit.decorators import ratelimit

from apps.accounts.decorators import role_required
from apps.accounts.models import User
from apps.accounts.portal import render_portal
from apps.exams.models import Exam, ExamAttempt
from apps.exams.permissions import visible_attempt_exams_q

from .models import (
    ProctoringSession,
    ViolationClipFrame,
    ViolationLog,
    ViolationSnapshot,
)
from .services import (
    LOCKDOWN_VIOLATIONS,
    ReviewPermissionError,
    apply_violation_review,
    decode_frame,
    get_session_for_student,
    handle_violation,
    integrity_index,
    invalidate_attempt,
    risk_level,
    strike_message,
)
from .tasks import process_proctor_frame, push_ws, verify_id_card


def _parse_json_body(request):
    """Safely parse a request body as JSON. Returns (data, error_response_or_None)."""
    try:
        return json.loads(request.body or b"{}"), None
    except (ValueError, json.JSONDecodeError):
        return None, JsonResponse({"error": "Malformed JSON body."}, status=400)


def _client_id_verification_status(session):
    """Map session state to the status the exam client expects.

    After a failed face check with attempts remaining, the DB status stays
    ``pending`` (so the student can retry). The client needs an explicit
    ``retry`` signal to show the Retry button instead of keeping the spinner.
    """
    status = session.id_verification_status
    if status != ProctoringSession.IDVerificationStatus.PENDING:
        return status
    max_attempts = settings.PROCTORING.get("MAX_ID_VERIFICATION_ATTEMPTS", 2)
    if session.id_verification_attempts >= max_attempts:
        return status
    last = session.id_attempts.order_by("-pk").first()
    if last and last.status == last.Status.FAILED:
        return "retry"
    return status


def _id_verification_payload(session):
    """Status plus the reason behind it, so the client can word the retry.

    A face that did not match reads very differently to the student than a
    camera that showed no face or a model that was not reachable.
    """
    return {
        "id_verification_status": _client_id_verification_status(session),
        "id_verification_reason": session.last_id_outcome,
        "id_verification_attempts": session.id_verification_attempts,
        "id_verification_faults": session.id_verification_faults,
    }


def _validate_frame(frame_b64):
    """Reject junk before it reaches Redis/Celery. Returns an error response or None."""
    if not isinstance(frame_b64, str) or not frame_b64:
        return JsonResponse({"error": "Missing webcam frame."}, status=400)
    max_bytes = settings.PROCTORING.get("MAX_FRAME_BYTES", 1_500_000)
    # base64 inflates by ~4/3, so this bounds the decoded size without decoding.
    if len(frame_b64) > (max_bytes * 4) // 3 + 4:
        return JsonResponse({"error": "Frame too large."}, status=400)
    if not decode_frame(frame_b64):
        return JsonResponse({"error": "Frame is not valid base64 image data."}, status=400)
    return None


@role_required(User.Role.TEACHER, User.Role.ADMIN)
def flagged_sessions(request):
    audit_readonly = request.user.is_admin_user
    if audit_readonly:
        sessions_qs = ProctoringSession.objects.all()
        portal_nav = "proctoring_audit"
        page_title = "Proctoring Audit"
    else:
        sessions_qs = ProctoringSession.objects.filter(
            visible_attempt_exams_q(request.user, prefix="attempt__exam__")
        )
        portal_nav = "flagged"
        page_title = "Flagged Sessions"

    flagged = (
        sessions_qs.filter(strike_count__gt=0)
        .select_related("attempt__student", "attempt__exam__course")
        .prefetch_related(
            Prefetch(
                "violations",
                queryset=ViolationLog.objects.order_by("created_at"),
            )
        )
        .annotate(
            violation_count=Count("violations"),
            disputed_count=Count("violations", filter=Q(violations__is_disputed=True)),
        )
        .order_by("-attempt__started_at")
    )

    selected = None
    selected_id = request.GET.get("session")
    flagged_list = []
    for s in flagged:
        level, risk = risk_level(s)
        violations = list(s.violations.all())
        flagged_list.append(
            {
                "session": s,
                "risk_level": level,
                "risk_score": risk,
                "integrity": integrity_index(s),
                "violations": violations,
                "latest_violation": violations[-1] if violations else None,
            }
        )

    if selected_id and str(selected_id).isdigit():
        selected = get_object_or_404(flagged, pk=selected_id)
    elif flagged.exists():
        selected = flagged.first()

    timeline = []
    logs = []
    snapshots = []
    clip_frames_by_violation = {}
    if selected:
        # Per-strike clip frames (the lead-up burst the browser uploaded),
        # grouped by violation so each snapshot/row can expose its sequence.
        for clip_frame in (
            ViolationClipFrame.objects.filter(violation__session=selected)
            .order_by("violation__created_at", "sequence")
        ):
            clip_frames_by_violation.setdefault(clip_frame.violation_id, []).append(
                clip_frame.image.url
            )

        timeline = list(
            selected.violations.select_related("snapshot").order_by("created_at")
        )
        for v in timeline:
            v.clip_frame_urls = clip_frames_by_violation.get(v.pk, [])
            logs.append(
                f"[{v.created_at.strftime('%H:%M:%S')}] OBJ_DETECT: {v.violation_type.upper()} "
                f"(confidence: {v.confidence:.2f})"
            )
        snapshots = list(
            ViolationSnapshot.objects.filter(violation__session=selected)
            .select_related("violation")
            .order_by("violation__created_at")
        )
        for snap in snapshots:
            snap.clip_frame_urls = clip_frames_by_violation.get(snap.violation_id, [])

    snapshot_by_violation = {s.violation_id: s for s in snapshots}

    return render_portal(
        request,
        "partials/portal/flagged_sessions.html",
        {
            "flagged_list": flagged_list,
            "selected_session": selected,
            "selected_integrity": integrity_index(selected) if selected else None,
            "timeline": timeline,
            "logs": logs,
            "snapshots": snapshots,
            "snapshot_by_violation": snapshot_by_violation,
            "audit_readonly": audit_readonly,
            "portal_nav": portal_nav,
            "active_count": flagged.filter(
                attempt__status=ExamAttempt.Status.IN_PROGRESS
            ).count(),
        },
        page_title=page_title,
    )


def _attempt_id_key(group, request):
    """Rate-limit key derived from the attempt id in the JSON body.

    Falls back to the user id if the body can't be parsed, so a malicious
    client can't escape the limit by sending malformed payloads.
    """
    try:
        body = json.loads(request.body or b"{}")
        attempt_id = body.get("attempt_id")
        if attempt_id is not None:
            return f"attempt:{attempt_id}"
    except (ValueError, json.JSONDecodeError):
        pass
    return f"user:{getattr(request.user, 'pk', 'anon')}"


@require_POST
@role_required(User.Role.STUDENT)
@ratelimit(key="user", rate="20/m", method="POST", block=True)
def id_verify(request):
    data, err = _parse_json_body(request)
    if err:
        return err
    attempt_id = data.get("attempt_id")
    frame_b64 = data.get("frame_base64", "")
    if frame_err := _validate_frame(frame_b64):
        return frame_err
    try:
        session = get_session_for_student(attempt_id, request.user)
    except ProctoringSession.DoesNotExist:
        return JsonResponse({"error": "Proctoring session not found."}, status=404)

    attempt = session.attempt
    if attempt.status != ExamAttempt.Status.PENDING_ID:
        return JsonResponse(
            {
                "error": "Identity verification is only allowed before the exam starts.",
                "attempt_status": attempt.status,
                "id_verification_status": _client_id_verification_status(session),
            },
            status=409,
        )
    if session.id_verification_status == ProctoringSession.IDVerificationStatus.FAILED:
        return JsonResponse(
            {
                "error": "Identity verification already failed for this attempt.",
                "id_verification_status": "failed",
                "attempt_status": attempt.status,
            },
            status=409,
        )

    # Run inline when Celery eager so the client gets a result immediately
    # instead of waiting on a worker queue that may not be running.
    if settings.CELERY_TASK_ALWAYS_EAGER:
        verify_id_card(attempt_id, frame_b64)
        session.refresh_from_db()
        session.attempt.refresh_from_db()
        return JsonResponse(
            {
                "accepted": True,
                "attempt_status": session.attempt.status,
                **_id_verification_payload(session),
            }
        )

    verify_id_card.delay(attempt_id, frame_b64)
    return JsonResponse({"accepted": True, "queued": True})


@require_POST
@role_required(User.Role.STUDENT)
@ratelimit(key=_attempt_id_key, rate="120/m", method="POST", block=True)  # ~2 req / s per attempt
def process_frame(request):
    data, err = _parse_json_body(request)
    if err:
        return err
    attempt_id = data.get("attempt_id")
    frame_b64 = data.get("frame_base64", "")
    client_events = data.get("client_events", [])
    if not isinstance(client_events, list):
        client_events = []
    if frame_err := _validate_frame(frame_b64):
        return frame_err

    try:
        session = get_session_for_student(attempt_id, request.user)
    except ProctoringSession.DoesNotExist:
        return JsonResponse({"error": "Proctoring session not found."}, status=404)
    if session.attempt.status != ExamAttempt.Status.IN_PROGRESS:
        return JsonResponse({"error": "Exam not in progress"}, status=400)

    for event in client_events:
        if not isinstance(event, dict):
            continue
        vtype = LOCKDOWN_VIOLATIONS.get(event.get("type"))
        if vtype:
            result = handle_violation(session, vtype, lockdown_event=True)
            if result:
                push_ws(
                    attempt_id,
                    {
                        "type": "warning",
                        "strike": result["strike"],
                        "max_strikes": result["max_strikes"],
                        "violation_type": vtype,
                        "action": result["action"],
                        "violation_id": result["log"].pk,
                        "message": strike_message(vtype, result["strike"]),
                    },
                )
                if result.get("terminated") or (
                    result["action"] == ViolationLog.ActionTaken.TERMINATE
                ):
                    push_ws(
                        attempt_id,
                        {"type": "terminate", "reason": "Lockdown violation"},
                    )

    process_proctor_frame.delay(attempt_id, frame_b64)
    session.refresh_from_db()
    return JsonResponse(
        {
            "accepted": True,
            "session_strikes": session.strike_count,
            "max_strikes": session.max_strikes,
        }
    )


@require_POST
@role_required(User.Role.STUDENT)
@ratelimit(key=_attempt_id_key, rate="60/m", method="POST", block=True)
def client_event(request):
    data, err = _parse_json_body(request)
    if err:
        return err
    attempt_id = data.get("attempt_id")
    event_type = data.get("type")
    try:
        session = get_session_for_student(attempt_id, request.user)
    except ProctoringSession.DoesNotExist:
        return JsonResponse({"error": "Proctoring session not found."}, status=404)
    vtype = LOCKDOWN_VIOLATIONS.get(event_type)
    if not vtype:
        return JsonResponse({"ignored": True})
    if session.attempt.status != ExamAttempt.Status.IN_PROGRESS:
        return JsonResponse({"ignored": True, "reason": "Attempt not in progress."})

    # Dispatch through the strictness policy. At NONE the call is a no-op; at
    # LEVEL_1 it goes through the 3-strike rule (warnings, then terminate);
    # at LEVEL_2 the first offense terminates immediately.
    reason_label = {
        "tab_switch": "Tab switched during exam",
        "visibility_hidden": "Tab switched during exam",
        "focus_lost": "Window left during exam",
        "exit_fullscreen": "Fullscreen exited during exam",
    }.get(event_type, "Lockdown violation")
    result = handle_violation(
        session, vtype, reason=reason_label, lockdown_event=True
    )
    if result is None:
        return JsonResponse({"ignored": True, "reason": "Strictness=none"})

    terminated = bool(result.get("terminated")) or (
        result.get("action") == ViolationLog.ActionTaken.TERMINATE
    )
    violation_id = result["log"].pk
    push_ws(
        attempt_id,
        {
            "type": "warning",
            "strike": result["strike"],
            "max_strikes": result["max_strikes"],
            "violation_type": vtype,
            "action": result["action"],
            "reason": reason_label,
            "violation_id": violation_id,
            "message": strike_message(vtype, result["strike"]),
        },
    )
    if terminated:
        push_ws(attempt_id, {"type": "terminate", "reason": reason_label})
    session.refresh_from_db()
    return JsonResponse(
        {
            "terminated": terminated,
            "strike": result["strike"],
            "max_strikes": result["max_strikes"],
            "reason": reason_label,
            "strikes": session.strike_count,
            "violation_id": violation_id,
            "message": strike_message(vtype, result["strike"]),
            "action": result["action"],
        }
    )


@require_POST
@role_required(User.Role.STUDENT)
@ratelimit(key=_attempt_id_key, rate="10/m", method="POST", block=True)
def dispute_latest_violation(request):
    """Mark the student's most recent un-disputed violation as disputed.

    The strike warning dialog shows the latest violation; the student presses
    'File Dispute' to flag it for teacher review. Idempotent — re-pressing the
    button on an already-disputed log is a no-op.
    """
    data, err = _parse_json_body(request)
    if err:
        return err
    attempt_id = data.get("attempt_id")
    try:
        session = get_session_for_student(attempt_id, request.user)
    except ProctoringSession.DoesNotExist:
        return JsonResponse({"error": "Proctoring session not found."}, status=404)

    if session.attempt.status != ExamAttempt.Status.IN_PROGRESS:
        return JsonResponse(
            {"disputed": False, "reason": "Attempt is not in progress."}, status=409
        )

    violation = (
        session.violations.filter(is_disputed=False).order_by("-created_at").first()
    )
    if violation is None:
        return JsonResponse({"disputed": False, "reason": "No violation to dispute."})

    violation.is_disputed = True
    violation.disputed_at = timezone.now()
    violation.save(update_fields=["is_disputed", "disputed_at"])
    return JsonResponse(
        {
            "disputed": True,
            "violation_id": violation.pk,
            "violation_type": violation.violation_type,
            "strike_number": violation.strike_number,
        }
    )


def _student_violation_or_none(attempt_id, violation_id, user, *, require_in_progress=True):
    """Resolve a ViolationLog the student owns, or (None, error_response)."""
    try:
        session = get_session_for_student(attempt_id, user)
    except ProctoringSession.DoesNotExist:
        return None, JsonResponse(
            {"error": "Proctoring session not found."}, status=404
        )
    if require_in_progress and session.attempt.status != ExamAttempt.Status.IN_PROGRESS:
        return None, JsonResponse(
            {"error": "Attempt is not in progress."}, status=409
        )
    if not str(violation_id).isdigit():
        return None, JsonResponse({"error": "Violation not found."}, status=404)
    violation = ViolationLog.objects.filter(pk=violation_id, session=session).first()
    if violation is None:
        return None, JsonResponse({"error": "Violation not found."}, status=404)
    return violation, None


@require_POST
@role_required(User.Role.STUDENT)
@ratelimit(key=_attempt_id_key, rate="30/m", method="POST", block=True)
def violation_clip(request):
    """Store the short burst of frames the browser buffered around a strike.

    These are the raw lead-up frames (the "clip") a teacher reviews. Idempotent:
    a second upload for a violation that already has frames is ignored, so a
    flaky network retry can't double-store.
    """
    data, err = _parse_json_body(request)
    if err:
        return err
    violation, err = _student_violation_or_none(
        data.get("attempt_id"), data.get("violation_id"), request.user
    )
    if err:
        return err
    if violation.clip_frames.exists():
        return JsonResponse({"saved": 0, "skipped": True})

    frames = data.get("frames") or []
    if not isinstance(frames, list):
        return JsonResponse({"error": "frames must be a list."}, status=400)
    max_frames = settings.PROCTORING.get("CLIP_FRAME_COUNT", 6)
    max_bytes = settings.PROCTORING.get("CLIP_MAX_FRAME_BYTES", 300_000)
    saved = 0
    for index, frame_b64 in enumerate(frames[:max_frames]):
        try:
            frame_bytes = decode_frame(frame_b64)
        except (ValueError, TypeError):
            continue
        if not frame_bytes or len(frame_bytes) > max_bytes:
            continue
        clip_frame = ViolationClipFrame(violation=violation, sequence=index)
        clip_frame.image.save(
            f"clip_{violation.pk}_{index}_{uuid.uuid4().hex[:6]}.jpg",
            ContentFile(frame_bytes),
            save=True,
        )
        saved += 1
    return JsonResponse({"saved": saved})


@require_POST
@role_required(User.Role.STUDENT)
@ratelimit(key=_attempt_id_key, rate="30/m", method="POST", block=True)
def acknowledge_violation(request):
    """Record that the student saw and acknowledged the strike alert."""
    data, err = _parse_json_body(request)
    if err:
        return err
    violation, err = _student_violation_or_none(
        data.get("attempt_id"), data.get("violation_id"), request.user
    )
    if err:
        return err
    if violation.acknowledged_at is None:
        violation.acknowledged_at = timezone.now()
        violation.save(update_fields=["acknowledged_at"])
    return JsonResponse({"acknowledged": True, "violation_id": violation.pk})


@role_required(User.Role.STUDENT)
def session_status(request, attempt_id):
    try:
        session = get_session_for_student(attempt_id, request.user)
    except ProctoringSession.DoesNotExist:
        return JsonResponse({"error": "Proctoring session not found."}, status=404)
    violations_qs = session.violations.order_by("created_at")
    violations = [
        {
            "id": v.pk,
            "violation_type": v.violation_type,
            "strike_number": v.strike_number,
            "created_at": v.created_at.isoformat(),
            "message": strike_message(v.violation_type, v.strike_number),
        }
        for v in violations_qs
    ]
    attempt = session.attempt
    return JsonResponse(
        {
            "strike_count": session.strike_count,
            "max_strikes": session.max_strikes,
            **_id_verification_payload(session),
            "violations": violations,
            "attempt_status": attempt.status,
            "remaining_seconds": attempt.computed_remaining_seconds,
            "pause_reason": attempt.pause_reason,
        }
    )


def _violation_row_response(request, violation):
    """Re-render the timeline row for the just-edited violation.

    Used by HTMX so the row swaps in-place after the teacher saves changes.
    """
    return render(
        request,
        "partials/portal/violation_row.html",
        {"v": violation},
    )


@require_POST
@role_required(User.Role.TEACHER)
def review_violation(request, pk):
    """Teacher action: status / severity / note update on a single violation.

    Returns the re-rendered row partial for HTMX swap, or a JSON error if the
    teacher doesn't own the underlying exam.
    """
    violation = get_object_or_404(
        ViolationLog.objects.select_related(
            "session__attempt__exam__course__teacher"
        ),
        pk=pk,
    )
    try:
        apply_violation_review(
            violation,
            request.user,
            review_status=request.POST.get("review_status") or None,
            severity=request.POST.get("severity") or None,
            teacher_note=request.POST.get("teacher_note"),
        )
    except ReviewPermissionError as e:
        return JsonResponse({"error": str(e)}, status=403)
    except ValueError as e:
        return JsonResponse({"error": str(e)}, status=400)

    return _violation_row_response(request, violation)


@require_POST
@role_required(User.Role.TEACHER)
def invalidate_exam_attempt(request, pk):
    """Teacher action: nullify the entire attempt (Disqualify/Nullify).

    Forces score=0 via grade_attempt and returns the refreshed session header
    so HTMX can swap in the new banner without a full page reload.
    """
    attempt = get_object_or_404(
        ExamAttempt.objects.select_related("exam__course__teacher"),
        pk=pk,
    )
    reason = (request.POST.get("reason") or "").strip()
    try:
        invalidate_attempt(attempt, request.user, reason=reason)
    except ReviewPermissionError as e:
        return JsonResponse({"error": str(e)}, status=403)
    except ValueError as e:
        return JsonResponse({"error": str(e)}, status=400)

    session = attempt.proctoring_session
    return render(
        request,
        "partials/portal/flagged_session_header.html",
        {
            "attempt": attempt,
            "session": session,
            "selected_integrity": integrity_index(session),
        },
    )
