import secrets
import string
from datetime import timedelta

from django.conf import settings
from django.db import models, transaction
from django.utils import timezone

from apps.accounts.models import User
from apps.courses.models import Course


def generate_exam_code(length=8):
    alphabet = string.ascii_uppercase + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


class QuestionBank(models.Model):
    # NOTE: `status`/`submitted_at` were originally intended for a per-bank
    # approval flow but the design (SYSTEM_DESIGN.md §2.2) approves at the
    # Exam level instead. The fields are retained for backwards-compat but no
    # longer surfaced in the UI -- a future migration can drop them safely.
    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        PENDING = "pending", "Pending Admin Review"
        APPROVED = "approved", "Approved"
        REJECTED = "rejected", "Rejected"

    course = models.ForeignKey(Course, on_delete=models.CASCADE, related_name="question_banks")
    title = models.CharField(max_length=255)
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.DRAFT)
    submitted_at = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return self.title

    @property
    def total_marks(self):
        # Single aggregation query instead of pulling every question into Python.
        from django.db.models import Sum

        return self.questions.aggregate(total=Sum("marks"))["total"] or 0

    @property
    def complete_count(self):
        return self.questions.filter(is_complete=True).count()


class Question(models.Model):
    class QuestionType(models.TextChoices):
        MCQ = "mcq", "Multiple Choice"
        TRUE_FALSE = "true_false", "True/False"
        SHORT_ANSWER = "short_answer", "Short Answer"

    class Difficulty(models.TextChoices):
        EASY = "easy", "Easy"
        MEDIUM = "medium", "Medium"
        HARD = "hard", "Hard"

    question_bank = models.ForeignKey(
        QuestionBank, on_delete=models.CASCADE, related_name="questions"
    )
    text = models.TextField()
    question_type = models.CharField(max_length=20, choices=QuestionType.choices)
    section = models.CharField(max_length=120, default="Section A: Multiple Choice")
    order = models.PositiveIntegerField(default=0)
    options = models.JSONField(default=list, blank=True)
    correct_answer = models.JSONField(default=dict)
    marks = models.PositiveIntegerField(default=1)
    explanation = models.TextField(blank=True)
    is_complete = models.BooleanField(default=False)
    difficulty = models.CharField(
        max_length=10, choices=Difficulty.choices, default=Difficulty.MEDIUM
    )
    blooms_level = models.CharField(max_length=80, blank=True)
    module_tags = models.JSONField(default=list, blank=True)

    class Meta:
        ordering = ["order", "pk"]

    def __str__(self):
        return self.text[:80]

    def save(self, *args, **kwargs):
        if not self.pk:
            if self.question_type == self.QuestionType.SHORT_ANSWER:
                self.section = self.section or "Section B: Short Answer"
            elif self.question_type == self.QuestionType.TRUE_FALSE:
                self.section = self.section or "Section A: True / False"
            else:
                self.section = self.section or "Section A: Multiple Choice"
            last = (
                Question.objects.filter(question_bank=self.question_bank)
                .aggregate(models.Max("order"))
                .get("order__max")
            )
            if self.order == 0:
                self.order = (last or 0) + 1
        super().save(*args, **kwargs)


class Exam(models.Model):
    class ApprovalStatus(models.TextChoices):
        DRAFT = "draft", "Draft"
        PENDING = "pending", "Pending Admin Approval"
        APPROVED = "approved", "Approved"
        REJECTED = "rejected", "Rejected"

    class StrictnessLevel(models.TextChoices):
        # Practice / open-book: no lockdown enforcement, no ML violation reactions.
        NONE = "none", "Not strict — no proctoring checks"
        # Strike rule: warnings on tab-switch / focus-loss / fullscreen-exit
        # and ML-detected violations; exam terminates on the final strike
        # (max_strikes, default 5).
        LEVEL_1 = "level_1", "Level 1 — 5-strike tolerance"
        # Zero-tolerance: any violation terminates the exam immediately.
        LEVEL_2 = "level_2", "Level 2 — no second chances"

    course = models.ForeignKey(Course, on_delete=models.CASCADE, related_name="exams")
    title = models.CharField(max_length=255)
    description = models.TextField(blank=True)
    duration_minutes = models.PositiveIntegerField(default=60)
    total_marks = models.PositiveIntegerField(default=100)
    passing_marks = models.PositiveIntegerField(default=40)
    max_attempts = models.PositiveIntegerField(default=1, null=True, blank=True)
    shuffle_questions = models.BooleanField(default=True)
    show_results_immediately = models.BooleanField(default=True)
    requires_access_code = models.BooleanField(default=True)
    is_published = models.BooleanField(default=False)
    available_from = models.DateTimeField(null=True, blank=True)
    available_until = models.DateTimeField(null=True, blank=True)
    approval_status = models.CharField(
        max_length=20, choices=ApprovalStatus.choices, default=ApprovalStatus.DRAFT
    )
    submitted_for_approval_at = models.DateTimeField(null=True, blank=True)
    approved_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="approved_exams",
    )
    approved_at = models.DateTimeField(null=True, blank=True)
    admin_notes = models.TextField(blank=True)
    rejection_reason = models.TextField(blank=True)
    is_frozen = models.BooleanField(default=False)
    frozen_at = models.DateTimeField(null=True, blank=True)
    frozen_reason = models.CharField(max_length=255, blank=True)
    extra_time_minutes = models.PositiveIntegerField(default=0)
    strictness_level = models.CharField(
        max_length=16,
        choices=StrictnessLevel.choices,
        default=StrictnessLevel.LEVEL_1,
        help_text=(
            "Lockdown policy for this exam. Level 1 (default) follows the 5-strike "
            "rule; Level 2 is zero-tolerance; 'Not strict' disables all checks."
        ),
    )
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.title

    @property
    def effective_duration_minutes(self):
        return self.duration_minutes + self.extra_time_minutes

    def is_available(self):
        return self.availability_state() == "open"

    def availability_state(self, now=None):
        """Return: open, scheduled, ended, frozen, unpublished, not_approved, no_schedule."""
        now = now or timezone.now()
        if not self.is_published:
            return "unpublished"
        if self.approval_status != self.ApprovalStatus.APPROVED:
            return "not_approved"
        if self.is_frozen:
            return "frozen"
        # SYSTEM_DESIGN: an exam without a scheduled window is never "open".
        if not self.available_from or not self.available_until:
            return "no_schedule"
        if now < self.available_from:
            return "scheduled"
        if now > self.available_until:
            return "ended"
        return "open"

    def availability_state_label(self):
        labels = {
            "open": "Open now",
            "scheduled": "Scheduled — not started yet",
            "ended": "Window closed",
            "frozen": "Paused by administrator",
            "unpublished": "Not published",
            "not_approved": "Awaiting approval",
            "no_schedule": "Schedule not set",
        }
        return labels.get(self.availability_state(), "Not available")

    # Publication is the teacher's call after an admin approves (see
    # exams.views.exam_publish). Nothing else reminds them, so an approved exam
    # left unpublished would quietly never open. These helpers make that state
    # visible everywhere the teacher looks, louder as the window approaches.
    PUBLISH_SOON_HOURS = 24

    @property
    def awaits_publish(self):
        return (
            self.approval_status == self.ApprovalStatus.APPROVED
            and not self.is_published
        )

    def publish_urgency(self, now=None):
        """How pressing the missing publish is: '', 'later', 'soon', 'missed', 'expired'.

        ``missed`` means students should be sitting the exam right now and
        cannot — the failure this whole notion exists to catch. ``expired`` is
        that failure after the fact, when publishing alone can no longer fix it.
        """
        if not self.awaits_publish:
            return ""
        if not self.available_from or not self.available_until:
            return "later"
        now = now or timezone.now()
        if now > self.available_until:
            return "expired"
        if now >= self.available_from:
            return "missed"
        if self.available_from - now <= timedelta(hours=self.PUBLISH_SOON_HOURS):
            return "soon"
        return "later"

    def publish_urgency_label(self, now=None):
        return {
            "expired": (
                "Window closed while unpublished — students never saw it. "
                "Set a new schedule and resubmit for approval"
            ),
            "missed": "Window has opened — students cannot see it until you publish",
            "soon": "Window opens within a day — publish it",
            "later": "Approved — awaiting your publish",
        }.get(self.publish_urgency(now), "")


class ExamAccessCode(models.Model):
    exam = models.ForeignKey(Exam, on_delete=models.CASCADE, related_name="access_codes")
    code = models.CharField(max_length=16, unique=True, default=generate_exam_code)
    is_active = models.BooleanField(default=True)
    max_uses = models.PositiveIntegerField(null=True, blank=True)
    use_count = models.PositiveIntegerField(default=0)
    expires_at = models.DateTimeField(null=True, blank=True)
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def is_valid(self):
        if not self.is_active:
            return False
        if self.expires_at and timezone.now() > self.expires_at:
            return False
        if self.max_uses is not None and self.use_count >= self.max_uses:
            return False
        return True


class ExamCodeRedemption(models.Model):
    access_code = models.ForeignKey(
        ExamAccessCode, on_delete=models.CASCADE, related_name="redemptions"
    )
    student = models.ForeignKey(User, on_delete=models.CASCADE, related_name="exam_redemptions")
    redeemed_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ("access_code", "student")


class ExamQuestion(models.Model):
    exam = models.ForeignKey(Exam, on_delete=models.CASCADE, related_name="exam_questions")
    question = models.ForeignKey(Question, on_delete=models.CASCADE)
    order = models.PositiveIntegerField(default=0)
    marks_override = models.PositiveIntegerField(null=True, blank=True)

    class Meta:
        ordering = ["order"]
        unique_together = ("exam", "question")


class ExamAttempt(models.Model):
    class Status(models.TextChoices):
        PENDING_ID = "pending_id", "Pending ID Verification"
        IN_PROGRESS = "in_progress", "In Progress"
        PAUSED = "paused", "Paused"
        SUBMITTED = "submitted", "Submitted"
        TERMINATED = "terminated", "Terminated"
        EXPIRED = "expired", "Expired"

    class GradingStatus(models.TextChoices):
        AUTO_COMPLETE = "auto_complete", "Auto Complete"
        PENDING_MANUAL = "pending_manual", "Pending Manual Review"
        COMPLETE = "complete", "Complete"

    class PauseReason(models.TextChoices):
        """Why a PAUSED attempt is paused.

        Used so admin unfreeze doesn't accidentally resume attempts that were
        paused by the student's own WebSocket disconnect (those should resume
        only when the student reconnects).
        """

        FREEZE = "freeze", "Frozen by administrator"
        DISCONNECT = "disconnect", "Student disconnected"

    exam = models.ForeignKey(Exam, on_delete=models.CASCADE, related_name="attempts")
    student = models.ForeignKey(User, on_delete=models.CASCADE, related_name="exam_attempts")
    attempt_number = models.PositiveIntegerField(default=1)
    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.PENDING_ID
    )
    started_at = models.DateTimeField(auto_now_add=True)
    ended_at = models.DateTimeField(null=True, blank=True)
    remaining_seconds = models.PositiveIntegerField(null=True, blank=True)
    timer_paused_at = models.DateTimeField(null=True, blank=True)
    pause_reason = models.CharField(
        max_length=20, choices=PauseReason.choices, blank=True, default=""
    )
    disconnect_grace_seconds = models.PositiveIntegerField(
        default=settings.PROCTORING["DISCONNECT_GRACE_SECONDS"]
    )
    score = models.DecimalField(max_digits=8, decimal_places=2, null=True, blank=True)
    percentage = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)
    passed = models.BooleanField(null=True, blank=True)
    grading_status = models.CharField(
        max_length=20,
        choices=GradingStatus.choices,
        default=GradingStatus.AUTO_COMPLETE,
    )
    termination_reason = models.TextField(blank=True)
    # Teacher-driven nullification (Disqualify/Nullify in the audit UI). When
    # True, grade_attempt forces score=0 and the student sees a banner on the
    # result page. Distinct from `status=TERMINATED`, which is automatic.
    is_invalidated = models.BooleanField(default=False)
    invalidation_reason = models.TextField(blank=True)
    invalidated_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="invalidated_attempts",
    )
    invalidated_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        indexes = [
            models.Index(fields=["exam", "student", "status"]),
        ]

    def __str__(self):
        return f"{self.student.username} — {self.exam.title} (#{self.attempt_number})"

    @property
    def computed_remaining_seconds(self) -> int:
        """Authoritative time-left in seconds.

        Derived from `started_at` + `effective_duration_minutes`, so the
        student can't manipulate the client clock. Frozen attempts (PAUSED)
        snapshot their remaining time at `timer_paused_at`.
        """
        total = self.exam.effective_duration_minutes * 60
        if self.status in (
            self.Status.SUBMITTED,
            self.Status.TERMINATED,
            self.Status.EXPIRED,
        ):
            return 0
        if self.status == self.Status.PAUSED and self.timer_paused_at:
            elapsed = (self.timer_paused_at - self.started_at).total_seconds()
        else:
            elapsed = (timezone.now() - self.started_at).total_seconds()
        return max(0, int(total - elapsed))


RESUMABLE_ATTEMPT_STATUSES = (
    ExamAttempt.Status.PENDING_ID,
    ExamAttempt.Status.IN_PROGRESS,
    ExamAttempt.Status.PAUSED,
)


def get_resumable_attempt(exam, student):
    """Return the student's open attempt for this exam, if any.

    Attempts whose identity check already failed are not resumable — the
    student must start a fresh attempt (subject to max_attempts). Ended
    statuses (submitted / terminated / expired) are excluded by the status
    filter below.
    """
    from apps.proctoring.models import ProctoringSession

    return (
        ExamAttempt.objects.filter(
            exam=exam,
            student=student,
            status__in=RESUMABLE_ATTEMPT_STATUSES,
        )
        .exclude(
            proctoring_session__id_verification_status=(
                ProctoringSession.IDVerificationStatus.FAILED
            )
        )
        .order_by("-started_at")
        .first()
    )


class Answer(models.Model):
    class ReviewStatus(models.TextChoices):
        AUTO_GRADED = "auto_graded", "Auto Graded"
        PENDING_REVIEW = "pending_review", "Pending Review"
        MANUALLY_GRADED = "manually_graded", "Manually Graded"

    attempt = models.ForeignKey(ExamAttempt, on_delete=models.CASCADE, related_name="answers")
    question = models.ForeignKey(Question, on_delete=models.CASCADE)
    selected_option = models.JSONField(null=True, blank=True)
    text_answer = models.TextField(blank=True)
    is_correct = models.BooleanField(null=True, blank=True)
    marks_awarded = models.DecimalField(max_digits=6, decimal_places=2, null=True, blank=True)
    review_status = models.CharField(
        max_length=20,
        choices=ReviewStatus.choices,
        default=ReviewStatus.AUTO_GRADED,
    )

    class Meta:
        unique_together = ("attempt", "question")


class ManualGrade(models.Model):
    answer = models.OneToOneField(Answer, on_delete=models.CASCADE, related_name="manual_grade")
    graded_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True)
    marks_awarded = models.DecimalField(max_digits=6, decimal_places=2)
    feedback = models.TextField(blank=True)
    graded_at = models.DateTimeField(auto_now_add=True)


class Result(models.Model):
    attempt = models.OneToOneField(ExamAttempt, on_delete=models.CASCADE, related_name="result")
    auto_score = models.DecimalField(max_digits=8, decimal_places=2, default=0)
    manual_score = models.DecimalField(max_digits=8, decimal_places=2, default=0)
    total_score = models.DecimalField(max_digits=8, decimal_places=2, default=0)
    max_score = models.DecimalField(max_digits=8, decimal_places=2, default=0)
    grade = models.CharField(max_length=10, blank=True)
    generated_at = models.DateTimeField(auto_now_add=True)
    finalized_at = models.DateTimeField(null=True, blank=True)


def grade_attempt(attempt):
    """Auto-grade MCQ/true-false; flag short answers for manual review.

    If the teacher has invalidated the attempt (Disqualify/Nullify from the
    audit UI), short-circuit: score = 0, passed = False, grading complete.
    """
    if attempt.is_invalidated:
        max_score = sum(
            (eq.marks_override or eq.question.marks)
            for eq in attempt.exam.exam_questions.select_related("question")
        )
        result, _ = Result.objects.get_or_create(attempt=attempt)
        result.auto_score = 0
        result.manual_score = 0
        result.total_score = 0
        result.max_score = max_score
        result.finalized_at = timezone.now()
        result.save()
        attempt.score = 0
        attempt.percentage = 0
        attempt.passed = False
        attempt.grading_status = ExamAttempt.GradingStatus.COMPLETE
        attempt.save()
        return

    auto_score = 0
    manual_pending = False
    max_score = 0

    for eq in attempt.exam.exam_questions.select_related("question"):
        question = eq.question
        marks = eq.marks_override or question.marks
        max_score += marks
        answer, _ = Answer.objects.get_or_create(attempt=attempt, question=question)

        if question.question_type in (
            Question.QuestionType.MCQ,
            Question.QuestionType.TRUE_FALSE,
        ):
            expected = question.correct_answer.get("value")
            given = (answer.selected_option or {}).get("value")
            is_correct = expected == given
            answer.is_correct = is_correct
            answer.marks_awarded = marks if is_correct else 0
            answer.review_status = Answer.ReviewStatus.AUTO_GRADED
            auto_score += answer.marks_awarded or 0
        else:
            answer.review_status = Answer.ReviewStatus.PENDING_REVIEW
            answer.is_correct = None
            answer.marks_awarded = None
            manual_pending = True
        answer.save()

    result, _ = Result.objects.get_or_create(attempt=attempt)
    result.auto_score = auto_score
    result.max_score = max_score
    result.total_score = auto_score + result.manual_score
    result.save()

    attempt.score = result.total_score
    attempt.percentage = (result.total_score / max_score * 100) if max_score else 0
    if manual_pending:
        attempt.grading_status = ExamAttempt.GradingStatus.PENDING_MANUAL
        attempt.passed = None
    else:
        attempt.grading_status = ExamAttempt.GradingStatus.COMPLETE
        attempt.passed = result.total_score >= attempt.exam.passing_marks
        result.finalized_at = timezone.now()
        result.save()
    attempt.save()
    return attempt
