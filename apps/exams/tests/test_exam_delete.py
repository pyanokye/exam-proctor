"""Exam deletion: ownership, attempt guard, and list UI."""

from __future__ import annotations

from datetime import timedelta

from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import StudentProfile, TeacherProfile, User
from apps.courses.models import Course
from apps.exams.models import Exam, ExamAttempt


class ExamDeleteTests(TestCase):
    def setUp(self):
        self.teacher_user = User.objects.create_user(
            username="delteach",
            email="delteach@example.com",
            password="pw",
            role=User.Role.TEACHER,
        )
        teacher = TeacherProfile.objects.create(
            user=self.teacher_user,
            staff_id_number="STF-DEL",
            approval_status=TeacherProfile.ApprovalStatus.APPROVED,
        )
        other_teacher_user = User.objects.create_user(
            username="otherteach",
            email="otherteach@example.com",
            password="pw",
            role=User.Role.TEACHER,
        )
        TeacherProfile.objects.create(
            user=other_teacher_user,
            staff_id_number="STF-OTH",
            approval_status=TeacherProfile.ApprovalStatus.APPROVED,
        )
        self.other_teacher_user = other_teacher_user
        self.admin = User.objects.create_user(
            username="deladmin",
            email="deladmin@example.com",
            password="pw",
            role=User.Role.ADMIN,
        )
        self.student = User.objects.create_user(
            username="delstud",
            email="delstud@example.com",
            password="pw",
            role=User.Role.STUDENT,
        )
        StudentProfile.objects.create(
            user=self.student, student_id_number="20888001"
        )
        self.course = Course.objects.create(
            code="DEL101", title="Delete Course", teacher=teacher
        )
        now = timezone.now()
        self.exam = Exam.objects.create(
            course=self.course,
            title="Deletable Exam",
            duration_minutes=30,
            total_marks=10,
            passing_marks=4,
            is_published=False,
            available_from=now + timedelta(days=1),
            available_until=now + timedelta(days=2),
            approval_status=Exam.ApprovalStatus.DRAFT,
            created_by=self.teacher_user,
        )
        self.client = Client(SERVER_NAME="localhost")
        self.url = reverse("exams:delete", kwargs={"pk": self.exam.pk})

    def test_teacher_can_delete_own_draft_exam(self):
        self.client.force_login(self.teacher_user)
        res = self.client.post(self.url)
        self.assertRedirects(res, reverse("exams:list"))
        self.assertFalse(Exam.objects.filter(pk=self.exam.pk).exists())

    def test_admin_can_delete_any_exam_without_attempts(self):
        self.client.force_login(self.admin)
        res = self.client.post(self.url)
        self.assertRedirects(res, reverse("exams:list"))
        self.assertFalse(Exam.objects.filter(pk=self.exam.pk).exists())

    def test_other_teacher_cannot_delete(self):
        self.client.force_login(self.other_teacher_user)
        res = self.client.post(self.url)
        self.assertEqual(res.status_code, 403)
        self.assertTrue(Exam.objects.filter(pk=self.exam.pk).exists())

    def test_student_cannot_delete(self):
        self.client.force_login(self.student)
        res = self.client.post(self.url)
        self.assertEqual(res.status_code, 403)
        self.assertTrue(Exam.objects.filter(pk=self.exam.pk).exists())

    def test_get_is_rejected(self):
        self.client.force_login(self.teacher_user)
        res = self.client.get(self.url)
        self.assertEqual(res.status_code, 405)
        self.assertTrue(Exam.objects.filter(pk=self.exam.pk).exists())

    def test_blocked_when_attempts_exist(self):
        ExamAttempt.objects.create(
            exam=self.exam,
            student=self.student,
            attempt_number=1,
            status=ExamAttempt.Status.SUBMITTED,
            remaining_seconds=0,
            ended_at=timezone.now(),
        )
        self.client.force_login(self.teacher_user)
        res = self.client.post(self.url)
        self.assertRedirects(
            res, reverse("exams:detail", kwargs={"pk": self.exam.pk})
        )
        self.assertTrue(Exam.objects.filter(pk=self.exam.pk).exists())

    def test_list_shows_delete_button(self):
        self.client.force_login(self.teacher_user)
        res = self.client.get(reverse("exams:list"))
        self.assertContains(res, "Delete")
        self.assertContains(res, self.url)
