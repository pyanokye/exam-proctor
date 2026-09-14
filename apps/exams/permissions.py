from django.core.exceptions import PermissionDenied
from django.db.models import Q
from django.shortcuts import get_object_or_404

from .models import Exam, Question, QuestionBank


def user_can_manage_course(user, course) -> bool:
    if user.is_admin_user:
        return True
    if not user.is_teacher_user:
        return False
    profile = getattr(user, "teacher_profile", None)
    return profile is not None and course.teacher_id == profile.pk


def user_can_view_attempts(user, exam) -> bool:
    """Who may read a student's answers, score and proctoring record.

    Deliberately narrower than `user_can_manage_course`: admins are excluded.
    Student work belongs to the teacher running the class, not to whoever
    administers the platform. Admins keep exam approval and the read-only
    proctoring audit, neither of which exposes answers.

    Both the exam's author and the course owner qualify. In the normal flow
    they are the same person, since `ExamForm` only offers a teacher their own
    courses, but they diverge for admin-authored exams (where only the course
    owner is a teacher) and for reassigned courses.
    """
    if not getattr(user, "is_teacher_user", False):
        return False
    if exam.created_by_id and exam.created_by_id == user.pk:
        return True
    profile = getattr(user, "teacher_profile", None)
    return profile is not None and exam.course.teacher_id == profile.pk


def visible_attempt_exams_q(user, prefix="") -> Q:
    """Queryset form of `user_can_view_attempts`, for list views.

    `prefix` is the ORM path from the queryset's model to Exam, e.g.
    `"attempt__exam__"` when filtering ProctoringSession.
    """
    return Q(**{f"{prefix}created_by": user}) | Q(
        **{f"{prefix}course__teacher__user": user}
    )


def get_bank_for_user(user, bank_id) -> QuestionBank:
    bank = get_object_or_404(QuestionBank.objects.select_related("course__teacher__user"), pk=bank_id)
    if not user_can_manage_course(user, bank.course):
        raise PermissionDenied
    return bank


def get_exam_for_user(user, exam_id) -> Exam:
    exam = get_object_or_404(Exam.objects.select_related("course__teacher__user"), pk=exam_id)
    if not user_can_manage_course(user, exam.course):
        raise PermissionDenied
    return exam


def get_exam_for_attempt_review(user, exam_id) -> Exam:
    """`get_exam_for_user` for pages that expose student work. No admin bypass."""
    exam = get_object_or_404(
        Exam.objects.select_related("course__teacher__user"), pk=exam_id
    )
    if not user_can_view_attempts(user, exam):
        raise PermissionDenied
    return exam


def get_question_for_user(user, bank_id, question_id) -> Question:
    bank = get_bank_for_user(user, bank_id)
    return get_object_or_404(Question, pk=question_id, question_bank=bank)
