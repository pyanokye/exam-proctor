"""Template context for the unified portal sidebar."""

import shutil

from django.conf import settings

from apps.accounts.models import StudentProfile, TeacherProfile
from apps.exams.models import Exam
from apps.proctoring.models import ProctoringSession


def _media_storage_pct() -> int:
    """Best-effort percent of disk used on the MEDIA_ROOT volume.

    Returns 0 if disk usage can't be sampled (e.g. running on a read-only FS
    in tests) so the sidebar never crashes.
    """
    try:
        path = str(getattr(settings, "MEDIA_ROOT", "/")) or "/"
        usage = shutil.disk_usage(path)
        if usage.total == 0:
            return 0
        return min(100, int(round((usage.used / usage.total) * 100)))
    except (OSError, ValueError):
        return 0


def portal_sidebar(request):
    if not getattr(request, "user", None) or not request.user.is_authenticated:
        return {}

    user = request.user
    if not (user.is_admin_user or user.is_teacher_user):
        return {}

    ctx = {
        "pending_teachers": 0,
        "pending_exams": 0,
        "pending_student_ids": 0,
        "published_exams": 0,
        "awaiting_publish": 0,
        "flagged_sessions": 0,
        # Admin sidebar shows disk usage of the MEDIA_ROOT volume.
        "storage_used_pct": _media_storage_pct() if user.is_admin_user else 0,
    }

    if user.is_admin_user:
        ctx["pending_teachers"] = TeacherProfile.objects.filter(
            approval_status=TeacherProfile.ApprovalStatus.PENDING
        ).count()
        ctx["pending_exams"] = Exam.objects.filter(
            approval_status=Exam.ApprovalStatus.PENDING
        ).count()
        ctx["pending_student_ids"] = StudentProfile.objects.filter(
            id_review_status=StudentProfile.IDReviewStatus.PENDING
        ).count()
        ctx["published_exams"] = Exam.objects.filter(is_published=True).count()
        ctx["flagged_sessions"] = ProctoringSession.objects.filter(
            strike_count__gt=0
        ).count()
    elif user.is_teacher_user:
        # Teacher sidebar badge: flagged sessions for exams in their courses.
        ctx["flagged_sessions"] = ProctoringSession.objects.filter(
            strike_count__gt=0,
            attempt__exam__course__teacher__user=user,
        ).count()
        # Approved exams still waiting on the teacher's publish. Without this
        # count the step is easy to forget, and the exam never opens.
        ctx["awaiting_publish"] = Exam.objects.filter(
            course__teacher__user=user,
            approval_status=Exam.ApprovalStatus.APPROVED,
            is_published=False,
        ).count()

    return ctx
