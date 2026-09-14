"""Question bank section: shared portal shell + live answer-editor switching."""

from __future__ import annotations

from django.test import Client, TestCase
from django.urls import reverse

from apps.accounts.models import TeacherProfile, User
from apps.courses.models import Course
from apps.exams.models import Question, QuestionBank


class QuestionBankShellTests(TestCase):
    """Every page in the section must use the one shared header + sidebar."""

    def setUp(self):
        self.teacher_user = User.objects.create_user(
            username="qbteach",
            email="qbteach@example.com",
            password="pw",
            role=User.Role.TEACHER,
        )
        teacher = TeacherProfile.objects.create(
            user=self.teacher_user,
            staff_id_number="STF-QB",
            approval_status=TeacherProfile.ApprovalStatus.APPROVED,
        )
        self.course = Course.objects.create(
            code="QB101", title="Builder Course", teacher=teacher
        )
        self.bank = QuestionBank.objects.create(
            course=self.course, title="Unit 1 Bank", created_by=self.teacher_user
        )
        self.client = Client(SERVER_NAME="localhost")
        self.client.force_login(self.teacher_user)

    def _pages(self):
        return {
            "builder": reverse("exams:question_bank_builder", kwargs={"bank_id": self.bank.pk}),
            "library": reverse("exams:question_bank_library", kwargs={"bank_id": self.bank.pk}),
            "import": reverse("exams:question_bank_import", kwargs={"bank_id": self.bank.pk}),
            "create": reverse("exams:question_bank_create"),
        }

    def test_pages_use_shared_faculty_header_not_a_bespoke_one(self):
        for name, url in self._pages().items():
            with self.subTest(page=name):
                html = self.client.get(url).content.decode()
                self.assertIn("EXAM PROCTOR FACULTY PORTAL", html)
                self.assertNotIn("KNUST EXAM PROCTORING SYSTEM", html)

    def test_pages_render_exactly_one_sidebar(self):
        for name, url in self._pages().items():
            with self.subTest(page=name):
                html = self.client.get(url).content.decode()
                self.assertEqual(html.count('class="portal-sidebar"'), 1)

    def test_pages_keep_the_logout_control(self):
        """The old bespoke header omitted it, stranding teachers in the section."""
        for name, url in self._pages().items():
            with self.subTest(page=name):
                res = self.client.get(url)
                self.assertContains(res, reverse("accounts:logout"))

    def test_tabs_mark_the_current_page(self):
        res = self.client.get(self._pages()["library"])
        self.assertContains(res, 'aria-current="page"')
        self.assertContains(res, "Question Library")


class QuestionBuilderAnswerEditorTests(TestCase):
    def setUp(self):
        self.teacher_user = User.objects.create_user(
            username="qbteach2",
            email="qbteach2@example.com",
            password="pw",
            role=User.Role.TEACHER,
        )
        teacher = TeacherProfile.objects.create(
            user=self.teacher_user,
            staff_id_number="STF-QB2",
            approval_status=TeacherProfile.ApprovalStatus.APPROVED,
        )
        self.course = Course.objects.create(
            code="QB202", title="Editor Course", teacher=teacher
        )
        self.bank = QuestionBank.objects.create(
            course=self.course, title="Editor Bank", created_by=self.teacher_user
        )
        self.client = Client(SERVER_NAME="localhost")
        self.client.force_login(self.teacher_user)
        self.url = reverse("exams:question_bank_builder", kwargs={"bank_id": self.bank.pk})

    def _make(self, **kwargs):
        defaults = {
            "question_bank": self.bank,
            "text": "Sample question",
            "question_type": Question.QuestionType.MCQ,
            "options": [{"key": "A", "label": "One"}, {"key": "B", "label": "Two"}],
            "correct_answer": {"value": "A"},
            "marks": 2,
        }
        defaults.update(kwargs)
        return Question.objects.create(**defaults)

    def test_all_three_answer_editors_are_present(self):
        """They must all ship so the type dropdown can swap them without a reload."""
        self._make()
        html = self.client.get(self.url).content.decode()
        for panel in ("mcq", "true_false", "short_answer"):
            self.assertIn(f'data-qb-answer-panel="{panel}"', html)

    def test_only_the_matching_editor_is_visible_without_js(self):
        self._make(
            question_type=Question.QuestionType.SHORT_ANSWER,
            options=[],
            correct_answer={"reference": "Because."},
        )
        html = self.client.get(self.url).content.decode()
        self.assertIn('data-qb-answer-panel="mcq" hidden', html)
        self.assertIn('data-qb-answer-panel="true_false" hidden', html)
        self.assertIn('data-qb-answer-panel="short_answer" >', html)

    def test_short_answer_question_still_gets_mcq_starter_rows(self):
        """Switching to MCQ in the browser needs two options ready to edit."""
        self._make(
            question_type=Question.QuestionType.SHORT_ANSWER,
            options=[],
            correct_answer={"reference": "Because."},
        )
        res = self.client.get(self.url)
        self.assertGreaterEqual(len(res.context["mcq_options"]), 2)
        self.assertContains(res, 'name="option_label_0"')

    def test_true_false_options_are_not_reused_as_mcq_rows(self):
        self._make(
            question_type=Question.QuestionType.TRUE_FALSE,
            options=[{"key": "true", "label": "True"}, {"key": "false", "label": "False"}],
            correct_answer={"value": "true"},
        )
        res = self.client.get(self.url)
        keys = [opt["key"] for opt in res.context["mcq_options"]]
        self.assertNotIn("true", keys)
        self.assertEqual(keys[:2], ["A", "B"])

    def test_stale_type_switch_hint_is_gone(self):
        self._make()
        res = self.client.get(self.url)
        self.assertNotContains(res, "Save after changing question type")

    def test_saving_as_short_answer_clears_mcq_options(self):
        """Disabled panels post nothing, so a type switch must not keep options."""
        question = self._make()
        res = self.client.post(
            f"{self.url}?q={question.pk}",
            {
                "action": "save",
                "text": "Explain the halting problem.",
                "question_type": Question.QuestionType.SHORT_ANSWER,
                "section": "Section B: Short Answer",
                "marks": 5,
                "explanation": "",
                "difficulty": Question.Difficulty.MEDIUM,
                "blooms_level": "",
                "tags_text": "",
                "correct_reference": "A decision problem with no general algorithm.",
            },
        )
        self.assertEqual(res.status_code, 302)
        question.refresh_from_db()
        self.assertEqual(question.question_type, Question.QuestionType.SHORT_ANSWER)
        self.assertEqual(question.options, [])
        self.assertEqual(
            question.correct_answer,
            {"reference": "A decision problem with no general algorithm."},
        )

    def test_saving_as_true_false_replaces_mcq_options(self):
        question = self._make()
        self.client.post(
            f"{self.url}?q={question.pk}",
            {
                "action": "save",
                "text": "Python is statically typed.",
                "question_type": Question.QuestionType.TRUE_FALSE,
                "section": "Section A: True / False",
                "marks": 1,
                "explanation": "",
                "difficulty": Question.Difficulty.EASY,
                "blooms_level": "",
                "tags_text": "",
                "correct_value": "false",
            },
        )
        question.refresh_from_db()
        self.assertEqual(question.question_type, Question.QuestionType.TRUE_FALSE)
        self.assertEqual(question.correct_answer, {"value": "false"})
