from celery import shared_task
from channels.layers import get_channel_layer
from asgiref.sync import async_to_sync

from django.utils import timezone

from apps.exams.models import Exam, ExamAttempt
from ml.id_verification.exam_verify import MATCH, NO_MATCH, UNAVAILABLE

from .models import IDVerificationAttempt, ProctoringSession, ViolationLog
from .services import (
    decode_frame,
    handle_violation,
    strike_message,
    update_look_away_state,
)

# An identity decision has been made; further rounds must not reopen it.
RESOLVED_ID_STATUSES = (
    ProctoringSession.IDVerificationStatus.PASSED,
    ProctoringSession.IDVerificationStatus.FAILED,
    ProctoringSession.IDVerificationStatus.UNVERIFIED,
)

VIOLATION_PRIORITY = {
    ViolationLog.ViolationType.PHONE: 0,
    ViolationLog.ViolationType.MULTIPLE_FACES: 1,
    ViolationLog.ViolationType.ABSENT: 2,
    ViolationLog.ViolationType.BOOK: 3,
    ViolationLog.ViolationType.NOTES: 4,
    ViolationLog.ViolationType.FACE_OBSTRUCTED: 5,
}


def push_ws(attempt_id, payload):
    channel_layer = get_channel_layer()
    if channel_layer:
        async_to_sync(channel_layer.group_send)(
            f"proctoring_{attempt_id}",
            {"type": "proctoring.event", "payload": payload},
        )


def _pick_violation(candidates):
    if not candidates:
        return None
    return min(
        candidates,
        key=lambda c: VIOLATION_PRIORITY.get(c.violation_type, 99),
    )


def _push_violation_event(attempt_id, result, violation_type):
    log = result.get("log")
    push_ws(
        attempt_id,
        {
            "type": "warning",
            "strike": result["strike"],
            "max_strikes": result["max_strikes"],
            "violation_type": violation_type,
            "action": result["action"],
            "violation_id": getattr(log, "pk", None),
            "message": strike_message(violation_type, result["strike"]),
            "terminated": bool(result.get("terminated")),
        },
    )
    if result.get("terminated") or (
        result.get("action") == ViolationLog.ActionTaken.TERMINATE
    ):
        push_ws(
            attempt_id,
            {"type": "terminate", "reason": "Maximum violations exceeded"},
        )


@shared_task
def process_proctor_frame(attempt_id, frame_b64):
    import logging

    logger = logging.getLogger(__name__)
    try:
        _process_proctor_frame_impl(attempt_id, frame_b64)
    except Exception:
        logger.exception("process_proctor_frame failed for attempt %s", attempt_id)


def _process_proctor_frame_impl(attempt_id, frame_b64):
    session = ProctoringSession.objects.select_related("attempt__exam").get(
        attempt_id=attempt_id
    )
    attempt = session.attempt
    if attempt.status != ExamAttempt.Status.IN_PROGRESS:
        return

    if attempt.exam.strictness_level == Exam.StrictnessLevel.NONE:
        return

    from django.conf import settings

    frame_bytes = decode_frame(frame_b64)

    if settings.PROCTORING.get("USE_MOCK_ML"):
        from .services import mock_yolo_detect

        detections = mock_yolo_detect(frame_bytes)
        session.last_frame_at = timezone.now()
        session.save(update_fields=["last_frame_at"])
        for det in detections:
            result = handle_violation(
                session,
                det["type"],
                confidence=det.get("confidence", 0.9),
            )
            if result:
                _push_violation_event(attempt_id, result, det["type"])
        return

    import logging

    logger = logging.getLogger(__name__)

    from ml.yolo_service.loader import get_detector, models_ready
    from ml.yolo_service.pipeline import analyze_frame
    from ml.yolo_service.snapshot import create_violation_snapshot

    get_detector()  # triggers (cached) load on first frame in this worker
    if not models_ready():
        # Do NOT analyze with a dead model: empty detections would falsely
        # increment the absent streak and strike the student for an
        # infrastructure failure. loader.py already logged the root cause.
        logger.error(
            "Skipping proctor frame for attempt %s: YOLO models unavailable.",
            attempt_id,
        )
        return

    analysis = analyze_frame(
        frame_bytes,
        absent_streak=session.absent_frame_streak,
        multi_person_streak=session.multi_person_frame_streak,
    )
    if analysis is None:
        logger.warning(
            "Dropped undecodable proctor frame for attempt %s (%d bytes).",
            attempt_id,
            len(frame_bytes or b""),
        )
        return

    now = timezone.now()
    session.last_frame_at = now
    session.absent_frame_streak = analysis.absent_streak
    session.multi_person_frame_streak = analysis.multi_person_streak

    # Sustained look-away timer: a strike only fires once the student has been
    # looking away continuously for LOOK_AWAY_SECONDS. We measure with real
    # elapsed wall-clock time between frames so the rule is independent of the
    # exact frame cadence. ``look_away_struck`` stops one long episode from
    # firing repeatedly; it clears the moment they look back.
    look_away_seconds = settings.PROCTORING.get("LOOK_AWAY_SECONDS", 5)
    (
        session.look_away_started_at,
        session.look_away_struck,
        look_away_strike_due,
    ) = update_look_away_state(
        session.look_away_started_at,
        session.look_away_struck,
        analysis.looking_away,
        now,
        look_away_seconds,
    )

    session.save(
        update_fields=[
            "last_frame_at",
            "absent_frame_streak",
            "multi_person_frame_streak",
            "look_away_started_at",
            "look_away_struck",
        ]
    )

    candidate = _pick_violation(analysis.violations)
    if candidate:
        result = handle_violation(
            session,
            candidate.violation_type,
            confidence=float(candidate.confidence),
            severity=candidate.severity,
        )
        if result:
            # Warn the student first: a snapshot failure must never block the
            # strike notification for a violation that is already committed.
            _push_violation_event(attempt_id, result, candidate.violation_type)
            try:
                create_violation_snapshot(result["log"], analysis)
            except Exception:
                logger.exception(
                    "Snapshot failed for violation %s", result["log"].pk
                )
            session.refresh_from_db()

    if look_away_strike_due:
        result = handle_violation(
            session,
            ViolationLog.ViolationType.FACE_OBSTRUCTED,
            confidence=0.75,
            severity=ViolationLog.Severity.MEDIUM,
        )
        if result:
            _push_violation_event(
                attempt_id, result, ViolationLog.ViolationType.FACE_OBSTRUCTED
            )
            try:
                create_violation_snapshot(result["log"], analysis)
            except Exception:
                logger.exception(
                    "Snapshot failed for violation %s", result["log"].pk
                )


@shared_task
def verify_id_card(attempt_id, frame_b64):
    """ID + face verification during exam start.

    Never raises: an unexpected error must not strand the student on the
    "VERIFYING…" screen, so any failure pushes a ``retry`` status instead.
    """
    import logging

    logger = logging.getLogger(__name__)
    try:
        _verify_id_card_impl(attempt_id, frame_b64)
    except Exception:
        logger.exception("verify_id_card crashed for attempt %s", attempt_id)
        try:
            push_ws(
                attempt_id,
                {"type": "id_status", "status": "retry", "reason": UNAVAILABLE},
            )
        except Exception:
            logger.exception("Could not push retry status for attempt %s", attempt_id)


def _run_face_check(frame_bytes, student, student_profile, logger):
    """Compare the frame against the student's reference face.

    Returns an ``ExamVerifyResult``; a crash inside the ML stack becomes an
    ``unavailable`` outcome rather than an identity failure.
    """
    from django.conf import settings

    from ml.id_verification.exam_verify import ExamVerifyResult, verify_exam_id_frame

    if settings.PROCTORING.get("USE_MOCK_ML"):
        return ExamVerifyResult(
            passed=True,
            id_confidence=0.95,
            face_match_score=0.85,
            outcome=MATCH,
        )
    try:
        return verify_exam_id_frame(
            frame_bytes, student_profile=student_profile, user=student
        )
    except Exception as exc:  # noqa: BLE001 — a fault must not read as a mismatch
        logger.exception("ID verification raised for student %s", student.pk)
        return ExamVerifyResult(
            passed=False,
            id_confidence=0.0,
            face_match_score=0.0,
            errors=[f"Face verification error: {exc}"],
            outcome=UNAVAILABLE,
        )


def _verify_id_card_impl(attempt_id, frame_b64):
    import logging

    logger = logging.getLogger(__name__)

    from django.conf import settings
    from django.db import transaction

    with transaction.atomic():
        session = (
            ProctoringSession.objects.select_for_update()
            .select_related("attempt__student__student_profile")
            .get(attempt_id=attempt_id)
        )
        attempt = session.attempt
        # Refuse resurrect / mid-exam verify: only PENDING_ID may proceed.
        if attempt.status != ExamAttempt.Status.PENDING_ID:
            logger.info(
                "ID verify refused for attempt %s: status=%s",
                attempt_id,
                attempt.status,
            )
            return
        if session.id_verification_status in RESOLVED_ID_STATUSES:
            logger.info(
                "ID verify refused for attempt %s: already %s",
                attempt_id,
                session.id_verification_status,
            )
            return

    frame_bytes = decode_frame(frame_b64)
    student = session.attempt.student
    student_profile = getattr(student, "student_profile", None)
    if student_profile is None:
        # The registration scan lives on the user, so verification can still
        # run; the missing row is a data fault for staff to repair.
        logger.warning(
            "Student %s has no StudentProfile; verifying against the account photo. "
            "Run: manage.py repair_student_profiles",
            student.pk,
        )

    if not frame_bytes:
        logger.warning(
            "ID verify for attempt %s received an undecodable frame.", attempt_id
        )

    result = _run_face_check(frame_bytes, student, student_profile, logger)
    if result.errors:
        logger.info(
            "ID verify for attempt %s [%s]: %s",
            attempt_id,
            result.outcome,
            "; ".join(result.errors),
        )

    face_threshold = settings.PROCTORING.get("FACE_ID_MATCH_THRESHOLD", 0.35)
    attempt_record = IDVerificationAttempt.objects.create(
        session=session,
        id_confidence_score=result.id_confidence,
        face_match_score=result.face_match_score,
        id_check_passed=result.passed,
        # When no comparison happened, mirror the overall decision so audit
        # rows stay consistent.
        face_check_passed=(
            result.face_match_score >= face_threshold
            if result.face_match_score > 0.0
            else result.passed
        ),
        status=(
            IDVerificationAttempt.Status.PASSED
            if result.passed
            else IDVerificationAttempt.Status.FAILED
        ),
        outcome=result.outcome,
        notes="; ".join(result.errors),
    )
    if frame_bytes:
        from django.core.files.base import ContentFile

        attempt_record.frame_image.save(
            f"id_verify_{attempt_id}_{attempt_record.pk}.jpg",
            ContentFile(frame_bytes),
            save=True,
        )

    # Re-check status under lock before mutating — refuse resurrect races.
    with transaction.atomic():
        session = (
            ProctoringSession.objects.select_for_update()
            .select_related("attempt")
            .get(pk=session.pk)
        )
        attempt = session.attempt
        if attempt.status != ExamAttempt.Status.PENDING_ID:
            return
        if session.id_verification_status in RESOLVED_ID_STATUSES:
            return

        session.last_id_outcome = result.outcome
        if result.is_conclusive:
            session.id_verification_attempts += 1
        else:
            session.id_verification_faults += 1

        if result.passed:
            _admit(session, attempt, ProctoringSession.IDVerificationStatus.PASSED)
            push_ws(attempt_id, {"type": "id_status", "status": "passed"})
        elif result.outcome == NO_MATCH:
            max_attempts = settings.PROCTORING.get("MAX_ID_VERIFICATION_ATTEMPTS", 2)
            if session.id_verification_attempts >= max_attempts:
                session.id_verification_status = (
                    ProctoringSession.IDVerificationStatus.FAILED
                )
                attempt.status = ExamAttempt.Status.TERMINATED
                attempt.ended_at = timezone.now()
                attempt.save(update_fields=["status", "ended_at"])
                push_ws(attempt_id, {"type": "id_status", "status": "failed"})
            else:
                push_ws(
                    attempt_id,
                    {"type": "id_status", "status": "retry", "reason": NO_MATCH},
                )
        elif _should_admit_unverifiable(session, result.outcome, settings):
            # The system, not the student, is why no comparison happened. Let
            # them sit the exam and flag the session for staff review instead
            # of locking them out of an exam they are entitled to take.
            _admit(session, attempt, ProctoringSession.IDVerificationStatus.UNVERIFIED)
            logger.warning(
                "Attempt %s admitted without a face match after %s faults (%s).",
                attempt_id,
                session.id_verification_faults,
                result.outcome,
            )
            push_ws(attempt_id, {"type": "id_status", "status": "unverified"})
        else:
            push_ws(
                attempt_id,
                {"type": "id_status", "status": "retry", "reason": result.outcome},
            )

        session.save()


def _admit(session, attempt, status):
    session.id_verification_status = status
    session.lockdown_active = True
    # Stamp the admission time so take_exam can distinguish the immediate
    # post-verify reload from a later resume (which must re-verify).
    session.id_verified_at = timezone.now()
    attempt.status = ExamAttempt.Status.IN_PROGRESS
    attempt.save(update_fields=["status"])


def _should_admit_unverifiable(session, outcome, settings):
    """Whether repeated system faults should admit the student, flagged.

    Only ``unavailable`` counts: that means the server could not perform a
    comparison (no reference face, model unreachable), which the student cannot
    fix and must not be locked out for. ``no_face`` is different — the camera is
    working and the student can move into frame, so those rounds keep retrying.

    ``ADMIT_WHEN_UNVERIFIABLE`` off keeps everyone retrying instead: stricter,
    at the cost of stranding students whose reference face is missing.
    """
    if outcome != UNAVAILABLE:
        return False
    if not settings.PROCTORING.get("ADMIT_WHEN_UNVERIFIABLE", True):
        return False
    budget = settings.PROCTORING.get("MAX_ID_VERIFICATION_FAULTS", 3)
    return session.id_attempts.filter(outcome=UNAVAILABLE).count() >= budget
