"""Resuming an admitted exam must re-prove the student's identity.

The threat: one person clears the ID check, then hands the laptop to someone
else who continues the exam. The mitigation is that reopening / "Continue
Exam" / refreshing an already-admitted attempt sends it back through identity
verification before any question text is served again. The reload that happens
immediately after a pass (within the grace window) must NOT be treated as a
resume, or verification would loop forever.
"""

from __future__ import annotations

from datetime import timedelta
from unittest import mock

from django.conf import settings
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import StudentProfile, TeacherProfile, User
from apps.courses.models import Course
from apps.exams.models import Exam, ExamAttempt, ExamQuestion, Question, QuestionBank
from apps.proctoring.models import ProctoringSession


class ResumeReverificationTests(TestCase):
    def setUp(self):
        self.student = User.objects.create_user(
            username="rvstud",
            email="rv@example.com",
            password="pw",
            role=User.Role.STUDENT,
        )
        StudentProfile.objects.create(
            user=self.student, student_id_number="20899123", face_embedding=[0.1] * 8
        )
        teacher_user = User.objects.create_user(
            username="rvteach",
            email="rvteach@example.com",
            password="pw",
            role=User.Role.TEACHER,
        )
        teacher = TeacherProfile.objects.create(
            user=teacher_user,
            staff_id_number="STF-RV",
            approval_status=TeacherProfile.ApprovalStatus.APPROVED,
        )
        course = Course.objects.create(code="RV101", title="Resume", teacher=teacher)
        now = timezone.now()
        self.exam = Exam.objects.create(
            course=course,
            title="Resume Exam",
            duration_minutes=30,
            total_marks=10,
            passing_marks=4,
            max_attempts=5,
            is_published=True,
            requires_access_code=False,
            available_from=now - timedelta(hours=1),
            available_until=now + timedelta(days=1),
            approval_status=Exam.ApprovalStatus.APPROVED,
            strictness_level=Exam.StrictnessLevel.LEVEL_1,
            created_by=teacher_user,
        )
        bank = QuestionBank.objects.create(course=course, title="RV Bank", created_by=teacher_user)
        question = Question.objects.create(
            question_bank=bank,
            question_type=Question.QuestionType.MCQ,
            text="What is my name?",
            options=[{"value": "A", "text": "Papa"}, {"value": "B", "text": "Obolo"}],
            correct_answer={"value": "A"},
            marks=1,
        )
        ExamQuestion.objects.create(exam=self.exam, question=question, order=1, marks_override=1)

        self.attempt = ExamAttempt.objects.create(
            exam=self.exam,
            student=self.student,
            attempt_number=1,
            status=ExamAttempt.Status.IN_PROGRESS,
            remaining_seconds=self.exam.duration_minutes * 60,
        )
        self.session = ProctoringSession.objects.create(
            attempt=self.attempt,
            id_verification_status=ProctoringSession.IDVerificationStatus.PASSED,
        )
        self.client = Client(SERVER_NAME="localhost")
        self.client.force_login(self.student)

    def _take(self):
        return self.client.get(
            reverse("exams:take", kwargs={"attempt_id": self.attempt.pk})
        )

    def test_stale_admission_forces_reverification_on_resume(self):
        # Admitted well before the grace window -> a genuine resume.
        self.session.id_verified_at = timezone.now() - timedelta(minutes=5)
        self.session.save(update_fields=["id_verified_at"])

        res = self._take()
        self.assertEqual(res.status_code, 200)
        self.attempt.refresh_from_db()
        self.session.refresh_from_db()
        # Sent back through identity verification, with no question served.
        self.assertEqual(self.attempt.status, ExamAttempt.Status.PENDING_ID)
        self.assertEqual(
            self.session.id_verification_status,
            ProctoringSession.IDVerificationStatus.PENDING,
        )
        self.assertEqual(len(res.context["exam_questions"]), 0)
        self.assertNotContains(res, "What is my name?")

    def test_recent_admission_does_not_reverify(self):
        # The immediate post-verify reload: admitted moments ago.
        self.session.id_verified_at = timezone.now()
        self.session.save(update_fields=["id_verified_at"])

        res = self._take()
        self.assertEqual(res.status_code, 200)
        self.attempt.refresh_from_db()
        self.assertEqual(self.attempt.status, ExamAttempt.Status.IN_PROGRESS)
        self.assertEqual(len(res.context["exam_questions"]), 1)
        self.assertContains(res, "What is my name?")

    def test_null_admission_timestamp_is_treated_as_resume(self):
        # Legacy sessions (admitted before this feature) have no timestamp;
        # fail safe by re-verifying rather than trusting an unknown admission.
        self.session.id_verified_at = None
        self.session.save(update_fields=["id_verified_at"])

        self._take()
        self.attempt.refresh_from_db()
        self.assertEqual(self.attempt.status, ExamAttempt.Status.PENDING_ID)

    def test_disconnect_paused_attempt_reverifies_on_return(self):
        self.attempt.status = ExamAttempt.Status.PAUSED
        self.attempt.pause_reason = ExamAttempt.PauseReason.DISCONNECT
        self.attempt.timer_paused_at = timezone.now()
        self.attempt.save(
            update_fields=["status", "pause_reason", "timer_paused_at"]
        )
        self.session.id_verified_at = timezone.now() - timedelta(minutes=2)
        self.session.save(update_fields=["id_verified_at"])

        self._take()
        self.attempt.refresh_from_db()
        self.assertEqual(self.attempt.status, ExamAttempt.Status.PENDING_ID)
        self.assertEqual(self.attempt.pause_reason, "")

    def test_feature_flag_off_keeps_the_student_in_the_exam(self):
        self.session.id_verified_at = timezone.now() - timedelta(minutes=10)
        self.session.save(update_fields=["id_verified_at"])

        with mock.patch.dict(settings.PROCTORING, {"REVERIFY_ON_RESUME": False}):
            res = self._take()
        self.attempt.refresh_from_db()
        self.assertEqual(self.attempt.status, ExamAttempt.Status.IN_PROGRESS)
        self.assertContains(res, "What is my name?")

    def test_reverification_resets_the_attempt_counter(self):
        self.session.id_verified_at = timezone.now() - timedelta(minutes=5)
        self.session.id_verification_attempts = 2
        self.session.id_verification_faults = 1
        self.session.save(
            update_fields=[
                "id_verified_at",
                "id_verification_attempts",
                "id_verification_faults",
            ]
        )

        self._take()
        self.session.refresh_from_db()
        self.assertEqual(self.session.id_verification_attempts, 0)
        self.assertEqual(self.session.id_verification_faults, 0)
