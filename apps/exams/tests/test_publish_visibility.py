"""An approved exam left unpublished must never fail silently.

Publication stays the teacher's call (see exams.views.exam_publish), so the only
thing standing between an approved exam and a missed sitting is whether the
teacher notices. These tests cover the reminders that make it noticeable.
"""

from __future__ import annotations

from datetime import timedelta

from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import TeacherProfile, User
from apps.courses.models import Course
from apps.exams.models import Exam


class PublishUrgencyTests(TestCase):
    def setUp(self):
        teacher_user = User.objects.create_user(
            username="pubteach",
            email="pubteach@example.com",
            password="pw",
            role=User.Role.TEACHER,
        )
        teacher = TeacherProfile.objects.create(
            user=teacher_user,
            staff_id_number="STF-PUB",
            approval_status=TeacherProfile.ApprovalStatus.APPROVED,
        )
        self.teacher_user = teacher_user
        self.course = Course.objects.create(code="PUB101", title="Publishing", teacher=teacher)

    def _exam(self, *, approval=Exam.ApprovalStatus.APPROVED, published=False, starts_in=None):
        now = timezone.now()
        start = None if starts_in is None else now + starts_in
        return Exam.objects.create(
            course=self.course,
            title="Publish Me",
            duration_minutes=30,
            total_marks=10,
            passing_marks=4,
            is_published=published,
            available_from=start,
            available_until=None if start is None else start + timedelta(hours=2),
            approval_status=approval,
            created_by=self.teacher_user,
        )

    def test_draft_exam_is_not_awaiting_publish(self):
        exam = self._exam(approval=Exam.ApprovalStatus.DRAFT, starts_in=timedelta(days=3))
        self.assertFalse(exam.awaits_publish)
        self.assertEqual(exam.publish_urgency(), "")

    def test_published_exam_is_not_awaiting_publish(self):
        exam = self._exam(published=True, starts_in=timedelta(days=3))
        self.assertEqual(exam.publish_urgency(), "")

    def test_distant_window_is_a_gentle_reminder(self):
        exam = self._exam(starts_in=timedelta(days=5))
        self.assertEqual(exam.publish_urgency(), "later")

    def test_window_within_a_day_escalates(self):
        exam = self._exam(starts_in=timedelta(hours=6))
        self.assertEqual(exam.publish_urgency(), "soon")

    def test_open_window_without_publish_is_the_failure_case(self):
        exam = self._exam(starts_in=timedelta(hours=-1))
        self.assertEqual(exam.publish_urgency(), "missed")
        self.assertIn("students cannot see it", exam.publish_urgency_label().lower())

    def test_closed_window_says_publishing_alone_cannot_fix_it(self):
        exam = self._exam(starts_in=timedelta(hours=-5))
        self.assertEqual(exam.publish_urgency(), "expired")
        self.assertIn("resubmit for approval", exam.publish_urgency_label())

    def test_approved_without_a_schedule_still_reminds(self):
        exam = self._exam(starts_in=None)
        self.assertEqual(exam.publish_urgency(), "later")


class PublishRemindersInUiTests(TestCase):
    def setUp(self):
        self.teacher_user = User.objects.create_user(
            username="uiteach",
            email="uiteach@example.com",
            password="pw",
            role=User.Role.TEACHER,
        )
        teacher = TeacherProfile.objects.create(
            user=self.teacher_user,
            staff_id_number="STF-UI",
            approval_status=TeacherProfile.ApprovalStatus.APPROVED,
        )
        course = Course.objects.create(code="UI101", title="UI", teacher=teacher)
        now = timezone.now()
        self.exam = Exam.objects.create(
            course=course,
            title="Awaiting Publish Exam",
            duration_minutes=30,
            total_marks=10,
            passing_marks=4,
            is_published=False,
            available_from=now + timedelta(hours=3),
            available_until=now + timedelta(hours=5),
            approval_status=Exam.ApprovalStatus.APPROVED,
            created_by=self.teacher_user,
        )
        self.client = Client(SERVER_NAME="localhost")
        self.client.force_login(self.teacher_user)

    def test_dashboard_lists_the_exam_awaiting_publish(self):
        res = self.client.get(reverse("accounts:dashboard"))
        self.assertContains(res, "waiting on you to publish")
        self.assertContains(res, "Awaiting Publish Exam")

    def test_sidebar_badge_counts_it(self):
        res = self.client.get(reverse("accounts:dashboard"))
        self.assertEqual(res.context["awaiting_publish"], 1)

    def test_exam_list_distinguishes_approved_from_draft(self):
        res = self.client.get(reverse("exams:list"))
        self.assertContains(res, "Approved — not published")

    def test_reminders_disappear_once_published(self):
        self.exam.is_published = True
        self.exam.save(update_fields=["is_published"])
        res = self.client.get(reverse("accounts:dashboard"))
        self.assertNotContains(res, "waiting on you to publish")
        self.assertEqual(res.context["awaiting_publish"], 0)

    def test_detail_page_explains_the_pending_publish(self):
        res = self.client.get(reverse("exams:detail", kwargs={"pk": self.exam.pk}))
        self.assertContains(res, "students cannot see it until it is published")
