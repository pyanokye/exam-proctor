"""Identity verification must fail only on a real mismatch.

A face that does not match is a decision about the person at the keyboard. No
face in the frame, an unreachable model or a missing reference face are the
check not happening — those must never spend the student's attempts or end an
exam they are entitled to sit.
"""

from __future__ import annotations

import base64
import shutil
import tempfile
from datetime import timedelta
from io import BytesIO
from unittest import mock

from django.conf import settings
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import StudentProfile, TeacherProfile, User
from apps.courses.models import Course
from apps.exams.models import Exam, ExamAttempt
from apps.proctoring.models import IDVerificationAttempt, ProctoringSession
from apps.proctoring.tasks import verify_id_card
from ml.id_verification.exam_verify import (
    MATCH,
    NO_FACE,
    NO_MATCH,
    UNAVAILABLE,
    ExamVerifyResult,
)

_MEDIA = tempfile.mkdtemp(prefix="id_flow_media_")

VERIFY_PATH = "ml.id_verification.exam_verify.verify_exam_id_frame"


def _jpeg_b64() -> str:
    from PIL import Image

    buf = BytesIO()
    Image.new("RGB", (64, 64), (90, 90, 90)).save(buf, format="JPEG")
    return base64.b64encode(buf.getvalue()).decode()


def _result(outcome, *, passed=False, score=0.0):
    return ExamVerifyResult(
        passed=passed,
        id_confidence=0.9 if passed else 0.0,
        face_match_score=score,
        errors=[] if passed else [f"simulated {outcome}"],
        outcome=outcome,
    )


@override_settings(MEDIA_ROOT=_MEDIA, CELERY_TASK_ALWAYS_EAGER=True)
class IdVerificationFlowTests(TestCase):
    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(_MEDIA, ignore_errors=True)

    def setUp(self):
        self.student = User.objects.create_user(
            username="flowstud",
            email="flow@example.com",
            password="pw",
            role=User.Role.STUDENT,
        )
        StudentProfile.objects.create(
            user=self.student,
            student_id_number="20899001",
            face_embedding=[0.1] * 8,
        )
        teacher_user = User.objects.create_user(
            username="flowteach",
            email="flowteach@example.com",
            password="pw",
            role=User.Role.TEACHER,
        )
        teacher = TeacherProfile.objects.create(
            user=teacher_user,
            staff_id_number="STF-FLOW",
            approval_status=TeacherProfile.ApprovalStatus.APPROVED,
        )
        course = Course.objects.create(code="FLW101", title="Flow", teacher=teacher)
        now = timezone.now()
        self.exam = Exam.objects.create(
            course=course,
            title="Flow Exam",
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
        self.attempt = ExamAttempt.objects.create(
            exam=self.exam,
            student=self.student,
            attempt_number=1,
            status=ExamAttempt.Status.PENDING_ID,
            remaining_seconds=self.exam.duration_minutes * 60,
        )
        self.session = ProctoringSession.objects.create(attempt=self.attempt)

    def _verify(self, outcome, *, passed=False, score=0.0, times=1):
        with mock.patch(VERIFY_PATH, return_value=_result(outcome, passed=passed, score=score)):
            for _ in range(times):
                verify_id_card(self.attempt.pk, _jpeg_b64())
        self.attempt.refresh_from_db()
        self.session.refresh_from_db()

    def test_no_face_never_counts_against_the_student(self):
        self._verify(NO_FACE, times=2)
        self.assertEqual(self.session.id_verification_attempts, 0)
        self.assertEqual(self.session.id_verification_faults, 2)
        self.assertEqual(self.attempt.status, ExamAttempt.Status.PENDING_ID)
        self.assertEqual(
            self.session.id_verification_status,
            ProctoringSession.IDVerificationStatus.PENDING,
        )

    def test_unavailable_admits_and_flags_once_the_fault_budget_is_spent(self):
        budget = settings.PROCTORING["MAX_ID_VERIFICATION_FAULTS"]
        self._verify(UNAVAILABLE, times=budget)
        self.assertEqual(
            self.session.id_verification_status,
            ProctoringSession.IDVerificationStatus.UNVERIFIED,
        )
        self.assertEqual(self.attempt.status, ExamAttempt.Status.IN_PROGRESS)
        self.assertTrue(self.session.lockdown_active)
        self.assertEqual(self.session.id_verification_attempts, 0)

    def test_strict_mode_keeps_retrying_instead_of_admitting(self):
        with mock.patch.dict(settings.PROCTORING, {"ADMIT_WHEN_UNVERIFIABLE": False}):
            self._verify(UNAVAILABLE, times=6)
        self.assertEqual(
            self.session.id_verification_status,
            ProctoringSession.IDVerificationStatus.PENDING,
        )
        self.assertEqual(self.attempt.status, ExamAttempt.Status.PENDING_ID)

    def test_mismatch_still_terminates_after_the_allowed_attempts(self):
        self._verify(NO_MATCH, score=0.11, times=settings.PROCTORING["MAX_ID_VERIFICATION_ATTEMPTS"])
        self.assertEqual(
            self.session.id_verification_status,
            ProctoringSession.IDVerificationStatus.FAILED,
        )
        self.assertEqual(self.attempt.status, ExamAttempt.Status.TERMINATED)
        self.assertIsNotNone(self.attempt.ended_at)

    def test_an_empty_frame_never_admits_however_often_it_repeats(self):
        self._verify(NO_FACE, times=6)
        self.assertEqual(
            self.session.id_verification_status,
            ProctoringSession.IDVerificationStatus.PENDING,
        )
        self.assertEqual(self.attempt.status, ExamAttempt.Status.PENDING_ID)

    def test_faults_do_not_bring_a_mismatch_closer_to_termination(self):
        self._verify(NO_FACE, times=4)
        self._verify(NO_MATCH, score=0.12)
        self.assertEqual(self.attempt.status, ExamAttempt.Status.PENDING_ID)
        self.assertEqual(self.session.id_verification_attempts, 1)

    def test_match_after_a_mismatch_starts_the_exam(self):
        self._verify(NO_MATCH, score=0.10)
        self._verify(MATCH, passed=True, score=0.71)
        self.assertEqual(
            self.session.id_verification_status,
            ProctoringSession.IDVerificationStatus.PASSED,
        )
        self.assertEqual(self.attempt.status, ExamAttempt.Status.IN_PROGRESS)

    def test_missing_profile_is_verified_against_the_account_photo(self):
        StudentProfile.objects.filter(user=self.student).delete()
        self.student.refresh_from_db()
        with mock.patch(VERIFY_PATH, return_value=_result(MATCH, passed=True, score=0.8)) as verify:
            verify_id_card(self.attempt.pk, _jpeg_b64())
        self.assertEqual(verify.call_args.kwargs["student_profile"], None)
        self.assertEqual(verify.call_args.kwargs["user"], self.student)
        self.attempt.refresh_from_db()
        self.assertEqual(self.attempt.status, ExamAttempt.Status.IN_PROGRESS)

    def test_every_round_is_recorded_for_the_audit_trail(self):
        self._verify(NO_FACE)
        record = IDVerificationAttempt.objects.get(session=self.session)
        self.assertEqual(record.outcome, NO_FACE)
        self.assertIn("simulated", record.notes)

    def test_status_endpoint_reports_the_reason(self):
        self._verify(NO_MATCH, score=0.09)
        client = Client(SERVER_NAME="localhost")
        client.force_login(self.student)
        res = client.get(
            reverse("proctoring:session_status", kwargs={"attempt_id": self.attempt.pk})
        )
        body = res.json()
        self.assertEqual(body["id_verification_status"], "retry")
        self.assertEqual(body["id_verification_reason"], NO_MATCH)
        self.assertEqual(body["id_verification_faults"], 0)

    def test_resolved_session_is_not_reopened_by_a_late_round(self):
        budget = settings.PROCTORING["MAX_ID_VERIFICATION_FAULTS"]
        self._verify(UNAVAILABLE, times=budget)
        self._verify(NO_MATCH, score=0.01)
        self.assertEqual(
            self.session.id_verification_status,
            ProctoringSession.IDVerificationStatus.UNVERIFIED,
        )
        self.assertEqual(self.attempt.status, ExamAttempt.Status.IN_PROGRESS)


class RepairStudentProfilesCommandTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="orphan",
            email="orphan@example.com",
            password="pw",
            role=User.Role.STUDENT,
        )

    def _run(self, **kwargs):
        from django.core.management import call_command

        with mock.patch("ml.face_id.service.face_id_available", return_value=True), mock.patch(
            "ml.face_id.service.embed_face", return_value=[0.2] * 512
        ), mock.patch(
            "ml.id_verification.exam_verify.reference_photo_bytes", return_value=b"jpeg"
        ):
            call_command("repair_student_profiles", **kwargs)

    def test_creates_the_missing_profile_and_embedding(self):
        self._run(user="orphan", student_id="20899777")
        profile = StudentProfile.objects.get(user=self.user)
        self.assertEqual(profile.student_id_number, "20899777")
        self.assertEqual(len(profile.face_embedding), 512)

    def test_dry_run_changes_nothing(self):
        self._run(user="orphan", student_id="20899778", dry_run=True)
        self.assertFalse(StudentProfile.objects.filter(user=self.user).exists())
