import base64
import binascii
from io import BytesIO

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from apps.exams.models import Exam, ExamAttempt, grade_attempt
from apps.exams.permissions import user_can_view_attempts

from .models import ProctoringSession, ViolationLog


def integrity_index(session):
    if not session.max_strikes:
        return 100
    risk = int((session.strike_count / session.max_strikes) * 89)
    return max(0, 100 - risk - 10)


def risk_level(session):
    score = 100 - integrity_index(session)
    if score >= 80:
        return "high", score
    if score >= 50:
        return "med", score
    return "low", score


LOCKDOWN_VIOLATIONS = {
    "tab_switch": ViolationLog.ViolationType.TAB_SWITCH,
    "focus_lost": ViolationLog.ViolationType.FOCUS_LOST,
    "exit_fullscreen": ViolationLog.ViolationType.EXIT_FULLSCREEN,
    "visibility_hidden": ViolationLog.ViolationType.TAB_SWITCH,
}

# Short, plain-language guidance shown to the student in the strike alert. Kept
# imperative ("do this") so the toast reads like a correction, not an accusation.
STRIKE_MESSAGE_DETAIL = {
    ViolationLog.ViolationType.FACE_OBSTRUCTED: "do not look away from the screen",
    ViolationLog.ViolationType.PHONE: "put your phone away",
    ViolationLog.ViolationType.BOOK: "remove books from view",
    ViolationLog.ViolationType.NOTES: "remove notes and secondary devices from view",
    ViolationLog.ViolationType.ABSENT: "stay visible to the camera",
    ViolationLog.ViolationType.MULTIPLE_FACES: "you must be alone during the exam",
    ViolationLog.ViolationType.TAB_SWITCH: "stay on the exam tab",
    ViolationLog.ViolationType.FOCUS_LOST: "keep the exam window focused",
    ViolationLog.ViolationType.EXIT_FULLSCREEN: "remain in fullscreen",
}


def strike_message(violation_type, strike) -> str:
    """Build the per-offence alert copy, e.g. 'Strike 1: do not look away from the screen'."""
    detail = STRIKE_MESSAGE_DETAIL.get(violation_type, "follow the exam rules")
    return f"Strike {strike}: {detail}"


def update_look_away_state(started_at, struck, looking_away, now, threshold_seconds):
    """Pure state transition for the sustained look-away timer.

    Tracks how long a student has been continuously looking away and decides
    when that crosses into a strike. Kept side-effect free so it can be unit
    tested without the ML pipeline or the database.

    Returns ``(started_at, struck, strike_due)``:
    - ``started_at``  — when the current look-away episode began (None if facing forward).
    - ``struck``      — whether this episode already produced a strike.
    - ``strike_due``  — True only on the single frame that crosses the threshold.
    """
    if looking_away:
        if started_at is None:
            return now, False, False
        if not struck and (now - started_at).total_seconds() >= threshold_seconds:
            return started_at, True, True
        return started_at, struck, False
    # Facing forward again — reset so the next episode times from scratch.
    return None, False, False


def get_session_for_student(attempt_id, user):
    # attempt_id arrives from client JSON — a non-numeric value must read as
    # "not found" (callers already handle DoesNotExist), not a 500.
    try:
        attempt_id = int(attempt_id)
    except (TypeError, ValueError):
        raise ProctoringSession.DoesNotExist
    return ProctoringSession.objects.select_related("attempt").get(
        attempt_id=attempt_id, attempt__student=user
    )


def record_violation(
    session,
    violation_type,
    confidence=1.0,
    synchronous=False,
    severity=None,
):
    """Increment strike count atomically; terminate if over max."""
    cooldown = settings.PROCTORING.get("VIOLATION_COOLDOWN_SECONDS", 10)
    if not synchronous:
        recent = session.violations.filter(
            violation_type=violation_type,
            created_at__gte=timezone.now() - timezone.timedelta(seconds=cooldown),
        ).exists()
        if recent:
            return None

    with transaction.atomic():
        session = ProctoringSession.objects.select_for_update().get(pk=session.pk)
        session.strike_count += 1
        strike = session.strike_count
        max_strikes = session.max_strikes
        # Three-strike rule: the Nth strike (where N == max_strikes) is the
        # terminating one. Strikes < max_strikes are warnings.
        action = (
            ViolationLog.ActionTaken.WARNING
            if strike < max_strikes
            else ViolationLog.ActionTaken.TERMINATE
        )
        log = ViolationLog.objects.create(
            session=session,
            violation_type=violation_type,
            confidence=confidence,
            severity=severity or ViolationLog.Severity.MEDIUM,
            strike_number=strike,
            action_taken=action,
        )
        session.save(update_fields=["strike_count"])

        if action == ViolationLog.ActionTaken.TERMINATE:
            attempt = session.attempt
            attempt.status = ExamAttempt.Status.TERMINATED
            attempt.termination_reason = f"Exceeded max strikes ({max_strikes})"
            attempt.ended_at = timezone.now()
            attempt.save()
            grade_attempt(attempt)

    return {
        "log": log,
        "strike": strike,
        "action": action,
        "max_strikes": max_strikes,
        "terminated": action == ViolationLog.ActionTaken.TERMINATE,
    }


def terminate_for_lockdown_violation(session, violation_type, reason):
    """Immediate termination for explicit lockdown events (tab switch, window
    blur, exit fullscreen). One violation = end the attempt.

    This is distinct from the three-strike rule applied to AI-detected
    violations: events here are unambiguous user actions, so we don't give a
    second chance — the design's "strict lockdown" policy.
    """
    with transaction.atomic():
        session = ProctoringSession.objects.select_for_update().get(pk=session.pk)
        attempt = session.attempt
        if attempt.status != ExamAttempt.Status.IN_PROGRESS:
            return None
        session.strike_count = session.max_strikes
        log = ViolationLog.objects.create(
            session=session,
            violation_type=violation_type,
            confidence=1.0,
            strike_number=session.strike_count,
            action_taken=ViolationLog.ActionTaken.TERMINATE,
        )
        session.save(update_fields=["strike_count"])
        attempt.status = ExamAttempt.Status.TERMINATED
        attempt.termination_reason = reason
        attempt.ended_at = timezone.now()
        attempt.save()
        grade_attempt(attempt)
    return {
        "log": log,
        "strike": session.strike_count,
        "max_strikes": session.max_strikes,
        "action": ViolationLog.ActionTaken.TERMINATE,
        "terminated": True,
    }


def handle_violation(
    session,
    violation_type,
    *,
    confidence=1.0,
    reason=None,
    lockdown_event=False,
    severity=None,
):
    """Single entry point that applies the exam's strictness policy.

    - StrictnessLevel.NONE -> no-op (no strikes, no termination).
    - StrictnessLevel.LEVEL_1 -> three-strike rule via ``record_violation``.
    - StrictnessLevel.LEVEL_2 -> immediate termination.

    ``lockdown_event`` distinguishes deliberate user actions (tab switch,
    fullscreen exit) from ML detections so the cooldown only applies to ML.
    """
    level = session.attempt.exam.strictness_level
    if level == Exam.StrictnessLevel.NONE:
        return None
    if level == Exam.StrictnessLevel.LEVEL_2:
        return terminate_for_lockdown_violation(
            session, violation_type, reason or "Lockdown policy: zero tolerance"
        )
    return record_violation(
        session,
        violation_type,
        confidence=confidence,
        synchronous=lockdown_event,
        severity=severity,
    )


class ReviewPermissionError(Exception):
    """Raised when a teacher tries to act on a violation/attempt they don't own."""


def _teacher_owns_attempt(user, attempt) -> bool:
    """A teacher can audit only attempts on exams they authored or own.

    Admins are deliberately excluded — flagged-session review is a teacher
    responsibility per the system design. Shares one rule with the exam
    results roster so the two can't drift apart.
    """
    return user_can_view_attempts(user, attempt.exam)


def apply_violation_review(
    violation,
    user,
    *,
    review_status=None,
    severity=None,
    teacher_note=None,
):
    """Apply teacher adjudication to a single ViolationLog.

    Only the teacher who owns the underlying exam may review. Each call sets
    ``reviewed_by`` + ``reviewed_at`` so the audit trail is fresh, and only
    actually mutates fields the caller passes in (None = leave alone).
    """
    attempt = violation.session.attempt
    if not _teacher_owns_attempt(user, attempt):
        raise ReviewPermissionError("You do not own this exam attempt.")

    updates = []
    if review_status is not None:
        if review_status not in ViolationLog.ReviewStatus.values:
            raise ValueError(f"Invalid review_status: {review_status!r}")
        violation.review_status = review_status
        updates.append("review_status")
    if severity is not None:
        if severity not in ViolationLog.Severity.values:
            raise ValueError(f"Invalid severity: {severity!r}")
        if violation.severity != severity:
            violation.severity = severity
            violation.severity_overridden = True
            updates.extend(["severity", "severity_overridden"])
    if teacher_note is not None:
        violation.teacher_note = teacher_note.strip()
        updates.append("teacher_note")

    if not updates:
        return violation

    violation.reviewed_by = user
    violation.reviewed_at = timezone.now()
    updates.extend(["reviewed_by", "reviewed_at"])

    with transaction.atomic():
        violation.save(update_fields=updates)
    return violation


def invalidate_attempt(attempt, user, *, reason):
    """Nullify an attempt: force score to zero on next grade run.

    Stamps ``invalidated_by/at`` and re-runs grading so the student's result
    page reflects the zero immediately. Idempotent — repeat invalidations
    only refresh the reason/audit fields.
    """
    if not _teacher_owns_attempt(user, attempt):
        raise ReviewPermissionError("You do not own this exam attempt.")
    reason = (reason or "").strip()
    if not reason:
        raise ValueError("Invalidation requires a reason.")

    with transaction.atomic():
        attempt.is_invalidated = True
        attempt.invalidation_reason = reason
        attempt.invalidated_by = user
        attempt.invalidated_at = timezone.now()
        attempt.save(
            update_fields=[
                "is_invalidated",
                "invalidation_reason",
                "invalidated_by",
                "invalidated_at",
            ]
        )
        grade_attempt(attempt)
    return attempt


def decode_frame(frame_base64):
    """Decode a data-URL or bare base64 frame. Returns b"" on any bad input
    (None, non-string, invalid base64) so callers never crash on client junk."""
    if not isinstance(frame_base64, str) or not frame_base64:
        return b""
    if "," in frame_base64:
        frame_base64 = frame_base64.split(",", 1)[1]
    try:
        return base64.b64decode(frame_base64)
    except (ValueError, binascii.Error):
        return b""


def mock_yolo_detect(frame_bytes):
    """Placeholder until Phase 3 ML integration."""
    return []
