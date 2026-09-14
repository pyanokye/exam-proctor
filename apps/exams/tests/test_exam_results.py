"""Teacher exam-results roster and the per-student responses modal."""

from __future__ import annotations

from datetime import timedelta

from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import StudentProfile, TeacherProfile, User
from apps.courses.models import Course
from apps.exams.models import (
    Answer,
    Exam,
    ExamAttempt,
    ExamQuestion,
    Question,
    QuestionBank,
    grade_attempt,
)


class ExamResultsTests(TestCase):
    def setUp(self):
        self.teacher_user = User.objects.create_user(
            username="resteach",
            email="resteach@example.com",
            password="pw",
            role=User.Role.TEACHER,
        )
        teacher = TeacherProfile.objects.create(
            user=self.teacher_user,
            staff_id_number="STF-RES",
            approval_status=TeacherProfile.ApprovalStatus.APPROVED,
        )
        self.other_teacher_user = User.objects.create_user(
            username="resother",
            email="resother@example.com",
            password="pw",
            role=User.Role.TEACHER,
        )
        TeacherProfile.objects.create(
            user=self.other_teacher_user,
            staff_id_number="STF-RES2",
            approval_status=TeacherProfile.ApprovalStatus.APPROVED,
        )
        self.admin = User.objects.create_user(
            username="resadmin",
            email="resadmin@example.com",
            password="pw",
            role=User.Role.ADMIN,
        )
        self.student = User.objects.create_user(
            username="resstud",
            email="resstud@example.com",
            password="pw",
            role=User.Role.STUDENT,
            first_name="Ada",
            last_name="Lovelace",
        )
        StudentProfile.objects.create(user=self.student, student_id_number="20999001")

        self.course = Course.objects.create(
            code="RES101", title="Results Course", teacher=teacher
        )
        now = timezone.now()
        self.exam = Exam.objects.create(
            course=self.course,
            title="Midterm",
            duration_minutes=30,
            total_marks=10,
            passing_marks=4,
            is_published=True,
            available_from=now - timedelta(days=1),
            available_until=now + timedelta(days=1),
            approval_status=Exam.ApprovalStatus.APPROVED,
            created_by=self.teacher_user,
        )
        bank = QuestionBank.objects.create(
            course=self.course, title="Bank", created_by=self.teacher_user
        )
        self.mcq = Question.objects.create(
            question_bank=bank,
            text="Which planet is closest to the sun?",
            question_type=Question.QuestionType.MCQ,
            options=[
                {"key": "A", "label": "Mercury"},
                {"key": "B", "label": "Venus"},
            ],
            correct_answer={"value": "A"},
            marks=5,
        )
        self.short = Question.objects.create(
            question_bank=bank,
            text="Explain orbital resonance.",
            question_type=Question.QuestionType.SHORT_ANSWER,
            correct_answer={},
            marks=5,
        )
        ExamQuestion.objects.create(exam=self.exam, question=self.mcq, order=1)
        ExamQuestion.objects.create(exam=self.exam, question=self.short, order=2)

        self.attempt = ExamAttempt.objects.create(
            exam=self.exam,
            student=self.student,
            attempt_number=1,
            status=ExamAttempt.Status.SUBMITTED,
            remaining_seconds=0,
            ended_at=timezone.now(),
        )
        Answer.objects.create(
            attempt=self.attempt, question=self.mcq, selected_option={"value": "B"}
        )
        Answer.objects.create(
            attempt=self.attempt,
            question=self.short,
            text_answer="Bodies exert regular gravitational tugs.",
        )
        grade_attempt(self.attempt)

        self.client = Client(SERVER_NAME="localhost")
        self.results_url = reverse("exams:exam_results", kwargs={"pk": self.exam.pk})
        self.responses_url = reverse(
            "exams:attempt_responses", kwargs={"attempt_id": self.attempt.pk}
        )

    # ── roster ──
    def test_owning_teacher_sees_student_in_roster(self):
        self.client.force_login(self.teacher_user)
        res = self.client.get(self.results_url)
        self.assertEqual(res.status_code, 200)
        self.assertContains(res, "Ada Lovelace")
        self.assertContains(res, self.responses_url)

    def test_roster_wires_modal_open_and_close_hooks(self):
        """The dialog is driven by delegated handlers in portal.js.

        Inline handlers would stop working after an HTMX history restore, and
        an inline close handler let the click reach the row underneath and
        re-open the modal.
        """
        self.client.force_login(self.teacher_user)
        res = self.client.get(self.results_url)
        html = res.content.decode()
        self.assertIn("data-attempt-open", html)
        self.assertIn("data-attempt-close", html)
        self.assertNotIn("showModal()", html)
        self.assertNotIn("closest('dialog')", html)

    def test_roster_reports_pending_manual_review(self):
        self.client.force_login(self.teacher_user)
        res = self.client.get(self.results_url)
        self.assertContains(res, "awaiting review")

    def test_roster_excludes_in_progress_attempts(self):
        other = User.objects.create_user(
            username="resstud2",
            email="resstud2@example.com",
            password="pw",
            role=User.Role.STUDENT,
            first_name="Grace",
            last_name="Hopper",
        )
        StudentProfile.objects.create(user=other, student_id_number="20999002")
        ExamAttempt.objects.create(
            exam=self.exam,
            student=other,
            attempt_number=1,
            status=ExamAttempt.Status.IN_PROGRESS,
        )
        self.client.force_login(self.teacher_user)
        res = self.client.get(self.results_url)
        self.assertNotContains(res, "Grace Hopper")
        self.assertEqual(res.context["summary"]["in_progress"], 1)

    def test_admin_forbidden_from_roster(self):
        """Student work is the teacher's. Admins approve exams, not marks."""
        self.client.force_login(self.admin)
        self.assertEqual(self.client.get(self.results_url).status_code, 403)

    def test_superuser_forbidden_from_roster(self):
        superuser = User.objects.create_superuser(
            username="ressuper", email="ressuper@example.com", password="pw"
        )
        self.client.force_login(superuser)
        self.assertEqual(self.client.get(self.results_url).status_code, 403)

    def test_other_teacher_forbidden(self):
        self.client.force_login(self.other_teacher_user)
        self.assertEqual(self.client.get(self.results_url).status_code, 403)

    def test_student_forbidden(self):
        self.client.force_login(self.student)
        self.assertEqual(self.client.get(self.results_url).status_code, 403)

    def test_course_owner_who_did_not_create_the_exam_may_view(self):
        """An admin-authored exam still belongs to the teacher running it."""
        self.exam.created_by = self.admin
        self.exam.save(update_fields=["created_by"])
        self.client.force_login(self.teacher_user)
        self.assertEqual(self.client.get(self.results_url).status_code, 200)

    def test_creator_who_no_longer_owns_the_course_may_view(self):
        """Reassigning the course must not orphan the author's own results."""
        self.course.teacher = self.other_teacher_user.teacher_profile
        self.course.save(update_fields=["teacher"])
        self.client.force_login(self.teacher_user)
        self.assertEqual(self.client.get(self.results_url).status_code, 200)

    # ── responses modal ──
    def test_responses_show_choice_correct_answer_and_text(self):
        self.client.force_login(self.teacher_user)
        res = self.client.get(self.responses_url)
        self.assertEqual(res.status_code, 200)
        self.assertContains(res, "Which planet is closest to the sun?")
        self.assertContains(res, "Bodies exert regular gravitational tugs.")

        rows = {r["question"].pk: r for r in res.context["rows"]}
        mcq_options = {o["key"]: o for o in rows[self.mcq.pk]["options"]}
        self.assertTrue(mcq_options["A"]["is_correct"])
        self.assertFalse(mcq_options["A"]["is_selected"])
        self.assertTrue(mcq_options["B"]["is_selected"])
        self.assertFalse(mcq_options["B"]["is_correct"])

    def test_responses_include_unanswered_questions(self):
        self.attempt.answers.filter(question=self.short).delete()
        self.client.force_login(self.teacher_user)
        res = self.client.get(self.responses_url)
        rows = {r["question"].pk: r for r in res.context["rows"]}
        self.assertIn(self.short.pk, rows)
        self.assertFalse(rows[self.short.pk]["answered"])
        self.assertContains(res, "No response recorded.")

    def test_responses_use_marks_override(self):
        ExamQuestion.objects.filter(exam=self.exam, question=self.mcq).update(
            marks_override=8
        )
        self.client.force_login(self.teacher_user)
        res = self.client.get(self.responses_url)
        rows = {r["question"].pk: r for r in res.context["rows"]}
        self.assertEqual(rows[self.mcq.pk]["max_marks"], 8)

    def test_responses_forbidden_for_other_teacher(self):
        self.client.force_login(self.other_teacher_user)
        self.assertEqual(self.client.get(self.responses_url).status_code, 403)

    def test_responses_forbidden_for_student(self):
        self.client.force_login(self.student)
        self.assertEqual(self.client.get(self.responses_url).status_code, 403)

    def test_responses_forbidden_for_admin(self):
        self.client.force_login(self.admin)
        self.assertEqual(self.client.get(self.responses_url).status_code, 403)

    def test_responses_reject_attempt_id_from_another_exam(self):
        """The exam is re-derived from the attempt, so guessing ids gains nothing."""
        other_course = Course.objects.create(
            code="RES202",
            title="Other Course",
            teacher=self.other_teacher_user.teacher_profile,
        )
        other_exam = Exam.objects.create(
            course=other_course,
            title="Other Midterm",
            duration_minutes=30,
            total_marks=10,
            passing_marks=4,
            created_by=self.other_teacher_user,
        )
        foreign_attempt = ExamAttempt.objects.create(
            exam=other_exam,
            student=self.student,
            attempt_number=1,
            status=ExamAttempt.Status.SUBMITTED,
            remaining_seconds=0,
            ended_at=timezone.now(),
        )
        self.client.force_login(self.teacher_user)
        url = reverse(
            "exams:attempt_responses", kwargs={"attempt_id": foreign_attempt.pk}
        )
        self.assertEqual(self.client.get(url).status_code, 403)

    def test_responses_are_not_cached(self):
        self.client.force_login(self.teacher_user)
        res = self.client.get(self.responses_url)
        self.assertIn("no-store", res["Cache-Control"])
