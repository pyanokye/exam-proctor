from django.db import models

from apps.exams.models import ExamAttempt


class ProctoringSession(models.Model):
    class IDVerificationStatus(models.TextChoices):
        PENDING = "pending", "Pending"
        PASSED = "passed", "Passed"
        FAILED = "failed", "Failed"
        # Admitted without a face match because the system could not perform
        # one (no reference on file, model unavailable). The student sits the
        # exam; the session is flagged so staff can review it afterwards.
        UNVERIFIED = "unverified", "Admitted Without Face Match"

    attempt = models.OneToOneField(
        ExamAttempt, on_delete=models.CASCADE, related_name="proctoring_session"
    )
    strike_count = models.PositiveIntegerField(default=0)
    max_strikes = models.PositiveIntegerField(default=5)
    id_verification_status = models.CharField(
        max_length=20,
        choices=IDVerificationStatus.choices,
        default=IDVerificationStatus.PENDING,
    )
    # When the current identity decision was last made (set on admit). Used to
    # tell the immediate post-verify page reload apart from a later *resume*,
    # so re-verification is only demanded when a student reopens/continues.
    id_verified_at = models.DateTimeField(null=True, blank=True)
    id_verification_attempts = models.PositiveIntegerField(
        default=0,
        help_text="Conclusive face comparisons only — system faults are not counted.",
    )
    id_verification_faults = models.PositiveIntegerField(
        default=0,
        help_text="Rounds that could not produce a comparison (no face, model or reference unavailable).",
    )
    last_id_outcome = models.CharField(
        max_length=20,
        blank=True,
        help_text="Outcome of the most recent identity round: match, no_match, no_face, unavailable.",
    )
    lockdown_active = models.BooleanField(default=False)
    last_frame_at = models.DateTimeField(null=True, blank=True)
    last_heartbeat_at = models.DateTimeField(null=True, blank=True)
    absent_frame_streak = models.PositiveIntegerField(
        default=0,
        help_text="Consecutive ML frames with no person detected (for absent rule).",
    )
    multi_person_frame_streak = models.PositiveIntegerField(
        default=0,
        help_text=(
            "Consecutive ML frames with 2+ persons detected (multiple-person "
            "rule only strikes after MULTIPLE_PERSON_CONSECUTIVE_FRAMES)."
        ),
    )
    # Sustained look-away tracking. A strike only fires once the student has
    # been looking away continuously for PROCTORING['LOOK_AWAY_SECONDS']; brief
    # glances reset the timer. ``look_away_struck`` prevents a single long
    # episode from generating repeated strikes (cleared when they look back).
    look_away_started_at = models.DateTimeField(null=True, blank=True)
    look_away_struck = models.BooleanField(default=False)

    def __str__(self):
        return f"Proctoring — attempt {self.attempt_id}"


class IDVerificationAttempt(models.Model):
    class Status(models.TextChoices):
        PASSED = "passed", "Passed"
        FAILED = "failed", "Failed"

    session = models.ForeignKey(
        ProctoringSession, on_delete=models.CASCADE, related_name="id_attempts"
    )
    frame_image = models.ImageField(upload_to="id_verification/", blank=True)
    detected_id_type = models.CharField(max_length=100, blank=True)
    id_confidence_score = models.FloatField(default=0)
    face_match_score = models.FloatField(default=0)
    id_check_passed = models.BooleanField(default=False)
    face_check_passed = models.BooleanField(default=False)
    status = models.CharField(max_length=20, choices=Status.choices)
    outcome = models.CharField(
        max_length=20,
        blank=True,
        help_text="match, no_match, no_face or unavailable.",
    )
    notes = models.TextField(
        blank=True,
        help_text="Why the round ended this way — shown to staff in the audit trail.",
    )
    created_at = models.DateTimeField(auto_now_add=True)


class ViolationLog(models.Model):
    class ViolationType(models.TextChoices):
        PHONE = "phone", "Mobile Phone"
        BOOK = "book", "Book"
        NOTES = "notes", "Notes"
        ABSENT = "absent", "Student Absent"
        MULTIPLE_FACES = "multiple_faces", "Multiple People"
        FACE_OBSTRUCTED = "face_obstructed", "Face Obstructed"
        TAB_SWITCH = "tab_switch", "Tab Switch"
        FOCUS_LOST = "focus_lost", "Focus Lost"
        EXIT_FULLSCREEN = "exit_fullscreen", "Exit Fullscreen"

    class ActionTaken(models.TextChoices):
        WARNING = "warning", "Warning"
        TERMINATE = "terminate", "Terminate"

    class Severity(models.TextChoices):
        LOW = "low", "Low"
        MEDIUM = "medium", "Medium"
        HIGH = "high", "High"
        CRITICAL = "critical", "Critical"

    class ReviewStatus(models.TextChoices):
        # Teacher adjudication state. PENDING_REVIEW is the default; the
        # teacher transitions to one of the other values via the audit UI.
        PENDING_REVIEW = "pending_review", "Pending Review"
        CONFIRMED = "confirmed", "Confirmed Violation"
        IGNORED = "ignored", "Ignored"
        FALSE_ALARM = "false_alarm", "Resolved — False Alarm"
        ESCALATED = "escalated", "Escalated"

    session = models.ForeignKey(
        ProctoringSession, on_delete=models.CASCADE, related_name="violations"
    )
    violation_type = models.CharField(max_length=30, choices=ViolationType.choices)
    confidence = models.FloatField(default=0)
    severity = models.CharField(
        max_length=20, choices=Severity.choices, default=Severity.MEDIUM
    )
    severity_overridden = models.BooleanField(
        default=False,
        help_text="True when a teacher manually changed the AI-assigned severity.",
    )
    strike_number = models.PositiveIntegerField(default=1)
    action_taken = models.CharField(max_length=20, choices=ActionTaken.choices)
    created_at = models.DateTimeField(auto_now_add=True)
    # Student-side dispute marker. Set when the student presses "File Dispute"
    # on the strike warning dialog. Teachers see it in the flagged-sessions
    # review and can choose how to weigh the violation.
    is_disputed = models.BooleanField(default=False)
    disputed_at = models.DateTimeField(null=True, blank=True)
    # Student acknowledged the strike alert. Recorded for the audit trail so a
    # teacher can see the student was warned and confirmed they saw it.
    acknowledged_at = models.DateTimeField(null=True, blank=True)
    # Teacher-side adjudication.
    review_status = models.CharField(
        max_length=20,
        choices=ReviewStatus.choices,
        default=ReviewStatus.PENDING_REVIEW,
    )
    teacher_note = models.TextField(
        blank=True,
        help_text="Optional teacher message shown to the student on their result page.",
    )
    reviewed_by = models.ForeignKey(
        "accounts.User",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="reviewed_violations",
    )
    reviewed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        indexes = [
            models.Index(fields=["session", "created_at"]),
            models.Index(fields=["review_status"]),
        ]

    @property
    def is_dismissed(self) -> bool:
        """A violation a teacher considers nullified for scoring discussions."""
        return self.review_status in (
            self.ReviewStatus.IGNORED,
            self.ReviewStatus.FALSE_ALARM,
        )


class ViolationSnapshot(models.Model):
    violation = models.OneToOneField(
        ViolationLog, on_delete=models.CASCADE, related_name="snapshot"
    )
    image = models.ImageField(upload_to="violations/")
    bounding_boxes = models.JSONField(default=list, blank=True)
    pose_keypoints = models.JSONField(default=list, blank=True)
    # Iris gaze at the moment of the strike, e.g. {"h_ratio": 0.31,
    # "v_ratio": 0.55, "off_screen": true}. Empty when gaze wasn't measured.
    gaze_metrics = models.JSONField(default=dict, blank=True)
    frame_width = models.PositiveIntegerField(default=0)
    frame_height = models.PositiveIntegerField(default=0)


class ViolationClipFrame(models.Model):
    """A frame from the short burst the browser uploads on each strike.

    Unlike ``ViolationSnapshot`` (a single server-annotated frame with bbox +
    pose), these are the raw lead-up frames the client buffered, stored in
    sequence so a teacher can scrub the moment that triggered the strike.
    """

    violation = models.ForeignKey(
        ViolationLog, on_delete=models.CASCADE, related_name="clip_frames"
    )
    image = models.ImageField(upload_to="violation_clips/")
    sequence = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["sequence"]
        indexes = [models.Index(fields=["violation", "sequence"])]
