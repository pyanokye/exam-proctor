from datetime import timedelta

from django.contrib import messages
from django.contrib.auth import login, logout
from django.contrib.auth.decorators import login_required
from django.contrib.auth.views import LoginView, LogoutView
from django.db.models import Avg, Count, Q
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse, reverse_lazy
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_POST
from django.views.generic import CreateView

from apps.accounts.decorators import role_required
from apps.accounts.forms import (
    AdminCreateAdminForm,
    AdminCreateStudentForm,
    AdminCreateTeacherForm,
    EmailOrUsernameAuthenticationForm,
    StudentRegistrationForm,
    TeacherRegistrationForm,
)
from apps.accounts.models import AdminProfile, StudentProfile, TeacherProfile, User
from apps.accounts.portal import hx_redirect, is_htmx, render_portal
from apps.courses.models import Course
from apps.exams.models import (
    Exam,
    ExamAttempt,
    ExamCodeRedemption,
    QuestionBank,
    get_resumable_attempt,
)


def _register_json_errors(form):
    errors = {}
    for field, msgs in form.errors.items():
        errors[field] = [str(m) for m in msgs]
    return errors


def home(request):
    if request.user.is_authenticated:
        return redirect("accounts:dashboard")
    return redirect("accounts:login")


# Forms that can simply be re-fetched with GET and submitted again.
CSRF_RETRY_PREFIXES = ("/login/student/", "/login/teacher/", "/login/admin/", "/register/")


def csrf_failure(request, reason="", template_name=""):
    """Recover from a stale CSRF token instead of Django's raw 403 page.

    Logging in or out rotates the token, so a page left open across that point
    still carries the old one. Its next POST (typically the header's Log out
    form) must land on a usable page rather than a dead end.
    """
    if any(request.path.startswith(prefix) for prefix in CSRF_RETRY_PREFIXES):
        target = request.path
        note = "That form expired before it was submitted. Please try again."
    elif request.user.is_authenticated:
        target = reverse("accounts:dashboard")
        note = "That page was out of date, so nothing was changed. Please try again."
    else:
        target = reverse("accounts:login")
        note = "Your session expired. Please sign in again."

    messages.add_message(request, messages.INFO, note, fail_silently=True)

    # CSRF rejection happens upstream of HtmxAuthRedirectMiddleware, so HTMX
    # requests need the full-page redirect header applied here.
    if is_htmx(request):
        return hx_redirect(target)
    return redirect(target)


def _admin_sidebar_context():
    pending = TeacherProfile.objects.filter(
        approval_status=TeacherProfile.ApprovalStatus.PENDING
    ).count()
    pending_exams = Exam.objects.filter(approval_status=Exam.ApprovalStatus.PENDING).count()
    pending_student_ids = StudentProfile.objects.filter(
        id_review_status=StudentProfile.IDReviewStatus.PENDING
    ).count()
    published = Exam.objects.filter(is_published=True).count()
    from apps.accounts.context_processors import _media_storage_pct

    return {
        "pending_teachers": pending,
        "pending_exams": pending_exams,
        "pending_student_ids": pending_student_ids,
        "published_exams": published,
        "storage_used_pct": _media_storage_pct(),
    }


def _teacher_review_source(request, profile):
    source = request.POST.get("from") or request.GET.get("from")
    if source in ("active", "pending"):
        return source
    if profile.approval_status == TeacherProfile.ApprovalStatus.APPROVED:
        return "active"
    return "pending"


def _teacher_review_meta(request, profile):
    source = _teacher_review_source(request, profile)
    if source == "active":
        return {
            "portal_nav": "active_teachers",
            "back_url": reverse("accounts:active_teachers"),
            "back_label": "← Active Teachers",
            "review_from": "active",
        }
    return {
        "portal_nav": "teachers",
        "back_url": reverse("accounts:pending_teachers"),
        "back_label": "← Teacher Approvals",
        "review_from": "pending",
    }


def _teacher_review_redirect_name(request, profile):
    source = _teacher_review_source(request, profile)
    return "accounts:active_teachers" if source == "active" else "accounts:pending_teachers"


def _build_admin_dashboard_context():
    now = timezone.now()
    week_ago = now - timedelta(days=7)
    sidebar = _admin_sidebar_context()

    total_users = User.objects.count()
    users_this_week = User.objects.filter(date_joined__gte=week_ago).count()

    active_exams = Exam.objects.filter(is_published=True).filter(
        Q(available_from__lte=now) | Q(available_from__isnull=True),
        Q(available_until__gte=now) | Q(available_until__isnull=True),
    ).count()
    dept_count = (
        Course.objects.exclude(teacher__department="")
        .values("teacher__department")
        .distinct()
        .count()
    )

    # Flagged-session triage now belongs to teachers (see proctoring.views).
    # The admin dashboard no longer surfaces per-session integrity data.

    teacher_rows = []
    pending_profiles = (
        TeacherProfile.objects.filter(
            approval_status=TeacherProfile.ApprovalStatus.PENDING
        )
        .select_related("user")
        .order_by("-user__date_joined")[:6]
    )
    for profile in pending_profiles:
        teacher_rows.append(
            {
                "profile": profile,
                "user": profile.user,
                "status": "pending",
                "staff_id": profile.staff_id_number or "—",
                "applied_at": profile.user.date_joined,
                "review_url": reverse("accounts:review_teacher", args=[profile.pk])
                + "?from=pending",
            }
        )

    active_teacher_rows = []
    approved_profiles = (
        TeacherProfile.objects.filter(
            approval_status=TeacherProfile.ApprovalStatus.APPROVED
        )
        .select_related("user", "approved_by")
        .order_by("-approved_at", "-user__date_joined")[:6]
    )
    for profile in approved_profiles:
        active_teacher_rows.append(
            {
                "profile": profile,
                "user": profile.user,
                "staff_id": profile.staff_id_number or "—",
                "approved_at": profile.approved_at or profile.user.date_joined,
                "is_active": profile.user.is_active,
                "review_url": reverse("accounts:review_teacher", args=[profile.pk])
                + "?from=active",
            }
        )

    submitted_statuses = [
        ExamAttempt.Status.SUBMITTED,
        ExamAttempt.Status.TERMINATED,
    ]
    exams = (
        Exam.objects.filter(is_published=True)
        .select_related("course", "course__teacher__user")
        .annotate(
            enrolled_count=Count("access_codes__redemptions", distinct=True),
            in_progress_count=Count(
                "attempts",
                filter=Q(attempts__status=ExamAttempt.Status.IN_PROGRESS),
            ),
            submitted_count=Count(
                "attempts",
                filter=Q(attempts__status__in=submitted_statuses),
            ),
            avg_score=Avg(
                "attempts__percentage",
                filter=Q(attempts__status__in=submitted_statuses),
            ),
        )
        .order_by("-created_at")
    )

    live_exams = []
    pending_exam_rows = []
    completed_exams = []

    for exam in exams:
        teacher_name = exam.course.teacher.user.get_full_name() or exam.course.teacher.user.username
        capacity = exam.enrolled_count or 1
        if exam.in_progress_count > 0:
            live_exams.append(
                {
                    "exam": exam,
                    "teacher_name": teacher_name,
                    "enrolled": exam.enrolled_count,
                    "capacity": max(capacity, exam.in_progress_count),
                    "in_progress": exam.in_progress_count,
                }
            )
        elif exam.available_from and exam.available_from > now:
            pending_exam_rows.append(
                {
                    "exam": exam,
                    "teacher_name": teacher_name,
                    "starts_at": exam.available_from,
                }
            )
        elif (exam.available_until and exam.available_until < now) or exam.submitted_count:
            completed_exams.append(
                {
                    "exam": exam,
                    "submitted": exam.submitted_count,
                    "avg_score": exam.avg_score,
                }
            )
        elif exam.is_available():
            live_exams.append(
                {
                    "exam": exam,
                    "teacher_name": teacher_name,
                    "enrolled": exam.enrolled_count,
                    "capacity": max(capacity, 1),
                    "in_progress": exam.in_progress_count,
                }
            )

    return {
        "portal_nav": "dashboard",
        "notification_count": sidebar["pending_teachers"] + sidebar["pending_exams"],
        "total_users": total_users,
        "users_this_week": users_this_week,
        "active_exams": active_exams,
        "dept_count": dept_count,
        "teacher_rows": teacher_rows,
        "active_teacher_rows": active_teacher_rows,
        "active_teachers_count": TeacherProfile.objects.filter(
            approval_status=TeacherProfile.ApprovalStatus.APPROVED,
            user__is_active=True,
        ).count(),
        "live_exams": live_exams[:3],
        "pending_exam_rows": pending_exam_rows[:2],
        "completed_exams": completed_exams[:2],
        "semester_label": now.strftime("%B %Y"),
    }


def _normalize_registration_role(role):
    return "teacher" if role == "teacher" else "student"


def register_choose(request):
    return render(request, "accounts/register_choose.html")


def register(request, role=None):
    if role is None:
        query_role = request.GET.get("role")
        if query_role in ("student", "teacher"):
            role = query_role
        elif request.method == "GET":
            return redirect("accounts:register")
        else:
            role = request.POST.get("role", "student")

    role = _normalize_registration_role(role)
    is_ajax = request.headers.get("X-Requested-With") == "XMLHttpRequest"
    register_url = reverse(
        "accounts:register_teacher" if role == "teacher" else "accounts:register_student"
    )

    if request.method == "POST":
        form_class = TeacherRegistrationForm if role == "teacher" else StudentRegistrationForm
        form = form_class(request.POST, request.FILES)
        if form.is_valid():
            user = form.save()
            if role == "teacher":
                if is_ajax:
                    return JsonResponse(
                        {
                            "success": True,
                            "redirect": reverse("accounts:login_teacher"),
                            "message": "Registration successful. Await admin approval before logging in.",
                        }
                    )
                messages.success(
                    request,
                    "Registration successful. Await admin approval before logging in.",
                )
                return redirect("accounts:login_teacher")
            login(request, user)
            student_profile = getattr(user, "student_profile", None)
            needs_review = (
                student_profile is not None
                and student_profile.id_review_status
                != StudentProfile.IDReviewStatus.APPROVED
            )
            if needs_review:
                redirect_url = reverse("accounts:id_review_pending")
                ajax_message = (
                    "Account created. We couldn't auto-verify your ID — "
                    "an administrator will review it."
                )
            else:
                redirect_url = reverse("accounts:dashboard")
                ajax_message = "Account verified — opening your dashboard…"
            if is_ajax:
                return JsonResponse(
                    {
                        "success": True,
                        "redirect": redirect_url,
                        "message": ajax_message,
                    }
                )
            return redirect(redirect_url)

        if is_ajax:
            return JsonResponse(
                {"success": False, "errors": _register_json_errors(form)},
                status=400,
            )
    else:
        form_class = TeacherRegistrationForm if role == "teacher" else StudentRegistrationForm
        form = form_class()

    return render(
        request,
        "accounts/register.html",
        {"form": form, "role": role, "register_url": register_url},
    )


def login_choose(request):
    """Role chooser landing page. Forwards ?next= to each role-specific login."""
    if request.user.is_authenticated and request.GET.get("switch") != "1":
        return redirect("accounts:dashboard")
    if request.GET.get("switch") == "1" and request.user.is_authenticated:
        logout(request)
        messages.info(request, "Signed out. Sign in with the account you want to use.")
    next_url = request.GET.get("next", "")
    return render(
        request,
        "accounts/login_choose.html",
        {"next_url": next_url},
    )


class RoleLoginView(LoginView):
    """Login view that authenticates only a specific role.

    Subclasses set ``allowed_role`` (a ``User.Role`` value) and ``role_label``
    (display name used in error messages and templates). Admin login also
    permits ``is_superuser`` users.
    """

    authentication_form = EmailOrUsernameAuthenticationForm
    redirect_authenticated_user = False

    allowed_role: str = ""
    role_label: str = ""
    role_slug: str = ""

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["role_label"] = self.role_label
        ctx["role_slug"] = self.role_slug
        return ctx

    def dispatch(self, request, *args, **kwargs):
        if (
            request.method == "GET"
            and request.GET.get("switch") == "1"
            and request.user.is_authenticated
        ):
            logout(request)
            messages.info(request, "Signed out. Sign in with the account you want to use.")
            return redirect(request.path)

        if request.method == "GET" and request.user.is_authenticated:
            return redirect(self.get_success_url())

        return super().dispatch(request, *args, **kwargs)

    def post(self, request, *args, **kwargs):
        if request.user.is_authenticated:
            logout(request)
        return super().post(request, *args, **kwargs)

    def _user_matches_role(self, user):
        if self.allowed_role == User.Role.ADMIN:
            return user.is_admin_user
        if self.allowed_role == User.Role.TEACHER:
            return user.is_teacher_user
        if self.allowed_role == User.Role.STUDENT:
            return user.is_student_user
        return False

    def form_valid(self, form):
        user = form.get_user()
        if not self._user_matches_role(user):
            actual_label = user.get_role_display() if hasattr(user, "get_role_display") else "another"
            form.add_error(
                None,
                f"This account isn't a {self.role_label} account. "
                f"Sign in via the {actual_label} portal instead.",
            )
            return self.form_invalid(form)

        if user.is_teacher_user:
            profile = getattr(user, "teacher_profile", None)
            if profile and profile.approval_status == TeacherProfile.ApprovalStatus.REJECTED:
                form.add_error(
                    None,
                    "Your teacher account was rejected. Contact the administrator.",
                )
                return self.form_invalid(form)
        return super().form_valid(form)


class StudentLoginView(RoleLoginView):
    template_name = "accounts/login_student.html"
    allowed_role = User.Role.STUDENT
    role_label = "Student"
    role_slug = "student"


class TeacherLoginView(RoleLoginView):
    template_name = "accounts/login_teacher.html"
    allowed_role = User.Role.TEACHER
    role_label = "Teacher"
    role_slug = "teacher"


class AdminLoginView(RoleLoginView):
    template_name = "accounts/login_admin.html"
    allowed_role = User.Role.ADMIN
    role_label = "Administrator"
    role_slug = "admin"


class CustomLogoutView(LogoutView):
    def get_default_redirect_url(self):
        return f"{reverse('accounts:login')}?switch=1"


@never_cache
@role_required(User.Role.ADMIN, User.Role.TEACHER, User.Role.STUDENT)
def dashboard(request):
    user = request.user
    if user.is_admin_user:
        return render_portal(
            request,
            "partials/portal/dashboard_admin.html",
            _build_admin_dashboard_context(),
            page_title="Admin Dashboard",
        )
    if user.is_teacher_user:
        exams = Exam.objects.filter(course__teacher__user=user)
        banks = (
            QuestionBank.objects.filter(course__teacher__user=user)
            .select_related("course")
            .annotate(question_count=Count("questions"))
            .order_by("-created_at")[:5]
        )
        # Approved but unpublished exams open for nobody. Surface them first,
        # most-urgent first, so the missing publish can't go unnoticed.
        awaiting_publish = sorted(
            exams.filter(
                approval_status=Exam.ApprovalStatus.APPROVED, is_published=False
            ).select_related("course"),
            key=lambda exam: (
                {"missed": 0, "expired": 1, "soon": 2, "later": 3}.get(
                    exam.publish_urgency(), 4
                ),
                exam.available_from or timezone.now(),
            ),
        )
        return render_portal(
            request,
            "partials/portal/dashboard_teacher.html",
            {
                "portal_nav": "dashboard",
                "exam_count": exams.count(),
                "published_count": exams.filter(is_published=True).count(),
                "awaiting_publish_exams": awaiting_publish,
                "banks": banks,
            },
            page_title="Teacher Dashboard",
        )
    # Exams the student can access via a redeemed code.
    redeemed_exam_ids = set(
        ExamCodeRedemption.objects.filter(student=user)
        .values_list("access_code__exam_id", flat=True)
    )

    # Collect all published exams this student is eligible to see:
    #   - open-access exams (requires_access_code=False) — visible to everyone
    #   - code-gated exams the student has already redeemed
    upcoming_qs = Exam.objects.filter(is_published=True).select_related("course")
    seen = set()
    upcoming = []
    for exam in upcoming_qs:
        if exam.pk in seen:
            continue
        seen.add(exam.pk)
        if not exam.requires_access_code or exam.pk in redeemed_exam_ids:
            upcoming.append(exam)

    active_exams = [e for e in upcoming if e.is_available()]
    active_exam_items = []
    for exam in active_exams:
        active_exam_items.append(
            {
                "exam": exam,
                "resume_attempt": get_resumable_attempt(exam, user),
            }
        )
    locked_exams = [
        {"exam": e, "availability": e.availability_state()}
        for e in upcoming
        if not e.is_available()
    ]

    past_attempts = (
        ExamAttempt.objects.filter(
            student=user,
            status__in=[ExamAttempt.Status.SUBMITTED, ExamAttempt.Status.TERMINATED],
        )
        .select_related("exam", "proctoring_session")
        .prefetch_related("proctoring_session__violations")
        .order_by("-started_at")[:4]
    )

    past_with_integrity = []
    for att in past_attempts:
        session = getattr(att, "proctoring_session", None)
        violation_count = session.violations.count() if session else 0
        max_strikes = session.max_strikes if session else 5
        integrity = max(0, int(100 - (violation_count / max(max_strikes, 1) * 40)))
        past_with_integrity.append(
            {
                "attempt": att,
                "violations": violation_count,
                "integrity": integrity,
            }
        )

    first_active = active_exam_items[0] if active_exam_items else None
    return render(
        request,
        "accounts/dashboard_student.html",
        {
            "active_exams": active_exams,
            "active_exam_items": active_exam_items,
            "locked_exams": locked_exams,
            "scheduled_count": len(upcoming),
            "active_exam": first_active["exam"] if first_active else None,
            "active_exam_resume": first_active["resume_attempt"] if first_active else None,
            "past_results": past_with_integrity,
        },
    )


@login_required
def pending_approval(request):
    """Landing page for teachers whose admin approval is still pending or rejected.

    Other roles get redirected to their dashboard so non-teachers don't see a
    confusing waiting screen.
    """
    user = request.user
    if not user.is_teacher_user:
        return redirect("accounts:dashboard")

    profile = getattr(user, "teacher_profile", None)
    if profile and profile.approval_status == profile.ApprovalStatus.APPROVED:
        return redirect("accounts:dashboard")

    context = {"profile": profile, "teacher_user": user}
    return render(request, "accounts/pending_approval.html", context)


@login_required
def id_review_pending(request):
    """Landing page for students whose ID has failed OCR and awaits admin review."""
    user = request.user
    if not user.is_student_user:
        return redirect("accounts:dashboard")

    profile = getattr(user, "student_profile", None)
    if (
        profile
        and profile.id_review_status == StudentProfile.IDReviewStatus.APPROVED
    ):
        return redirect("accounts:dashboard")

    verification = (profile.id_verification_data if profile else {}) or {}
    context = {
        "profile": profile,
        "student_user": user,
        "ocr_errors": verification.get("ocr_errors", []),
        "ocr_warnings": verification.get("ocr_warnings", []),
    }
    return render(request, "accounts/id_review_pending.html", context)


@role_required(User.Role.ADMIN)
def pending_teachers(request):
    teachers = (
        TeacherProfile.objects.filter(approval_status=TeacherProfile.ApprovalStatus.PENDING)
        .select_related("user")
        .order_by("-user__date_joined")
    )
    teacher_items = [
        {
            "profile": t,
            "review_url": reverse("accounts:review_teacher", args=[t.pk]) + "?from=pending",
        }
        for t in teachers
    ]
    sidebar = _admin_sidebar_context()
    context = {
        "portal_nav": "teachers",
        "teacher_items": teacher_items,
        "pending_count": teachers.count(),
        "dashboard_url": reverse("accounts:dashboard"),
        "notification_count": sidebar["pending_teachers"] + sidebar["pending_exams"] + sidebar["pending_student_ids"],
    }
    return render_portal(
        request,
        "partials/portal/pending_teachers.html",
        context,
        page_title="Teacher Approvals",
    )


@role_required(User.Role.ADMIN)
def active_teachers(request):
    teachers = (
        TeacherProfile.objects.filter(
            approval_status=TeacherProfile.ApprovalStatus.APPROVED,
        )
        .select_related("user", "approved_by")
        .order_by("-approved_at", "-user__date_joined")
    )
    teacher_items = [
        {
            "profile": t,
            "review_url": reverse("accounts:review_teacher", args=[t.pk]) + "?from=active",
        }
        for t in teachers
    ]
    sidebar = _admin_sidebar_context()
    context = {
        "portal_nav": "active_teachers",
        "teacher_items": teacher_items,
        "active_count": teachers.filter(user__is_active=True).count(),
        "revoke_next": reverse("accounts:active_teachers"),
        "dashboard_url": reverse("accounts:dashboard"),
        "notification_count": sidebar["pending_teachers"] + sidebar["pending_exams"] + sidebar["pending_student_ids"],
    }
    return render_portal(
        request,
        "partials/portal/active_teachers.html",
        context,
        page_title="Active Teachers",
    )


@role_required(User.Role.ADMIN)
def user_list(request):
    users = User.objects.order_by("-date_joined")
    sidebar = _admin_sidebar_context()
    return render_portal(
        request,
        "partials/portal/user_list.html",
        {
            "portal_nav": "users",
            "users": users,
            "notification_count": sidebar["pending_teachers"] + sidebar["pending_exams"] + sidebar["pending_student_ids"],
            "can_create_admin": request.user.is_superuser,
        },
        page_title="All Users",
    )


@require_POST
@role_required(User.Role.ADMIN)
def admin_user_revoke(request, pk):
    """Toggle a user's active state (revoke / reactivate access).

    Soft action — preserves data, audit trail, and any authored content.
    Hard deletion is restricted to the Django superuser admin.
    """
    target = get_object_or_404(User, pk=pk)
    actor = request.user

    if target.pk == actor.pk:
        messages.error(request, "You cannot revoke your own account.")
        return redirect("accounts:user_list")

    # Only the super admin may revoke / reactivate another admin.
    target_is_admin = target.role == User.Role.ADMIN or target.is_superuser
    if target_is_admin and not actor.is_superuser:
        messages.error(request, "Only the super administrator can revoke admin accounts.")
        return redirect("accounts:user_list")

    # Safety net: never disable the last active superuser.
    if target.is_active and target.is_superuser:
        remaining_supers = User.objects.filter(
            is_superuser=True, is_active=True
        ).exclude(pk=target.pk).count()
        if remaining_supers == 0:
            messages.error(
                request,
                "Refusing to revoke the last active super administrator.",
            )
            return redirect("accounts:user_list")

    target.is_active = not target.is_active
    target.save(update_fields=["is_active"])
    label = target.get_full_name() or target.username
    if target.is_active:
        messages.success(request, f"Reactivated access for {label}.")
    else:
        messages.success(request, f"Revoked access for {label}.")

    next_url = request.POST.get("next")
    if next_url and url_has_allowed_host_and_scheme(
        next_url,
        allowed_hosts={request.get_host()},
        require_https=request.is_secure(),
    ):
        return redirect(next_url)
    return redirect("accounts:user_list")


# Map of provisioning role -> (form class, page title, success message verb).
_ADMIN_USER_FORMS = {
    "student": (AdminCreateStudentForm, "Add Student", "Student"),
    "teacher": (AdminCreateTeacherForm, "Add Teacher", "Teacher"),
    "admin": (AdminCreateAdminForm, "Add Administrator", "Administrator"),
}


@role_required(User.Role.ADMIN)
def admin_user_create(request, role):
    role = role.lower()
    if role not in _ADMIN_USER_FORMS:
        messages.error(request, "Unknown role.")
        return redirect("accounts:user_list")

    # Only the super-admin (Django superuser) may provision new admins.
    if role == "admin" and not request.user.is_superuser:
        messages.error(request, "Only the super administrator can create admin accounts.")
        return redirect("accounts:user_list")

    form_cls, page_title, role_label = _ADMIN_USER_FORMS[role]

    if request.method == "POST":
        form = form_cls(request.POST)
        if form.is_valid():
            user = form.save()
            # Stamp approver metadata for teacher accounts (form already marks APPROVED).
            if role == "teacher":
                profile = user.teacher_profile
                profile.approved_by = request.user
                profile.approved_at = timezone.now()
                profile.save(update_fields=["approved_by", "approved_at"])
            messages.success(
                request,
                f"{role_label} account created for {user.get_full_name() or user.username}.",
            )
            return redirect("accounts:user_list")
    else:
        form = form_cls()

    sidebar = _admin_sidebar_context()
    return render_portal(
        request,
        "partials/portal/user_create.html",
        {
            "portal_nav": "users",
            "form": form,
            "role": role,
            "role_label": role_label,
            "page_title": page_title,
            "notification_count": sidebar["pending_teachers"] + sidebar["pending_exams"] + sidebar["pending_student_ids"],
        },
        page_title=page_title,
    )


@role_required(User.Role.ADMIN)
def review_teacher(request, pk):
    profile = get_object_or_404(
        TeacherProfile.objects.select_related("user", "approved_by"),
        pk=pk,
    )
    user = profile.user
    verification = profile.id_verification_data or {}
    matched = verification.get("matched_fields", {})
    sidebar = _admin_sidebar_context()
    review_meta = _teacher_review_meta(request, profile)
    review_return_url = (
        reverse("accounts:review_teacher", args=[profile.pk])
        + f"?from={review_meta['review_from']}"
    )

    if request.method == "POST":
        action = request.POST.get("action")
        redirect_name = _teacher_review_redirect_name(request, profile)
        if action == "approve":
            profile.approval_status = TeacherProfile.ApprovalStatus.APPROVED
            profile.approved_by = request.user
            profile.approved_at = timezone.now()
            profile.save()
            user.is_active = True
            user.save(update_fields=["is_active"])
            messages.success(request, f"Approved {user.get_full_name() or user.username} for the platform.")
            return redirect(redirect_name)
        if action == "reject":
            profile.approval_status = TeacherProfile.ApprovalStatus.REJECTED
            profile.save()
            messages.info(request, f"Rejected {user.get_full_name() or user.username}.")
            return redirect(redirect_name)

    return render_portal(
        request,
        "partials/portal/teacher_review.html",
        {
            **review_meta,
            "dashboard_url": reverse("accounts:dashboard"),
            "review_return_url": review_return_url,
            "profile": profile,
            "teacher_user": user,
            "matched_fields": matched,
            "ocr_preview": verification.get("ocr_preview", ""),
            "notification_count": sidebar["pending_teachers"] + sidebar["pending_exams"] + sidebar["pending_student_ids"],
        },
        page_title=f"Review — {user.get_full_name() or user.username}",
    )


@require_POST
@role_required(User.Role.ADMIN)
def approve_teacher(request, pk):
    profile = get_object_or_404(TeacherProfile.objects.select_related("user"), pk=pk)
    action = request.POST.get("action")
    if action == "approve":
        profile.approval_status = TeacherProfile.ApprovalStatus.APPROVED
        profile.approved_by = request.user
        profile.approved_at = timezone.now()
        profile.save()
        profile.user.is_active = True
        profile.user.save(update_fields=["is_active"])
        messages.success(request, f"Approved {profile.user.username}.")
    elif action == "reject":
        profile.approval_status = TeacherProfile.ApprovalStatus.REJECTED
        profile.save()
        messages.info(request, f"Rejected {profile.user.username}.")
    return redirect("accounts:pending_teachers")


@role_required(User.Role.ADMIN)
def pending_student_ids(request):
    """Queue of students whose ID failed OCR and needs manual review."""
    students = (
        StudentProfile.objects.filter(
            id_review_status=StudentProfile.IDReviewStatus.PENDING
        )
        .select_related("user")
        .order_by("-user__date_joined")
    )
    student_items = [
        {
            "profile": s,
            "review_url": reverse("accounts:review_student", args=[s.pk]),
        }
        for s in students
    ]
    sidebar = _admin_sidebar_context()
    context = {
        "portal_nav": "student_ids",
        "student_items": student_items,
        "pending_count": students.count(),
        "dashboard_url": reverse("accounts:dashboard"),
        "notification_count": sidebar["pending_teachers"]
        + sidebar["pending_student_ids"],
    }
    return render_portal(
        request,
        "partials/portal/pending_student_ids.html",
        context,
        page_title="Student ID Reviews",
    )


@role_required(User.Role.ADMIN)
def review_student(request, pk):
    """Admin review page for a single student's ID upload."""
    profile = get_object_or_404(
        StudentProfile.objects.select_related("user", "id_reviewed_by"),
        pk=pk,
    )
    user = profile.user
    verification = profile.id_verification_data or {}
    matched = verification.get("matched_fields", {}) or {}
    ocr_errors = verification.get("ocr_errors", []) or []
    sidebar = _admin_sidebar_context()

    if request.method == "POST":
        action = request.POST.get("action")
        if action == "approve":
            profile.id_review_status = StudentProfile.IDReviewStatus.APPROVED
            profile.id_card_verified = True
            profile.id_reviewed_by = request.user
            profile.id_reviewed_at = timezone.now()
            profile.id_review_notes = request.POST.get("review_notes", "")
            profile.save()
            messages.success(
                request,
                f"Approved {user.get_full_name() or user.username} — they can now take exams.",
            )
            return redirect("accounts:pending_student_ids")
        if action == "reject":
            profile.id_review_status = StudentProfile.IDReviewStatus.REJECTED
            profile.id_card_verified = False
            profile.id_reviewed_by = request.user
            profile.id_reviewed_at = timezone.now()
            profile.id_review_notes = request.POST.get(
                "review_notes",
                "Rejected by administrator.",
            )
            profile.save()
            messages.info(
                request,
                f"Rejected {user.get_full_name() or user.username}.",
            )
            return redirect("accounts:pending_student_ids")

    return render_portal(
        request,
        "partials/portal/student_review.html",
        {
            "portal_nav": "student_ids",
            "back_url": reverse("accounts:pending_student_ids"),
            "dashboard_url": reverse("accounts:dashboard"),
            "profile": profile,
            "student_user": user,
            "matched_fields": matched,
            "ocr_errors": ocr_errors,
            "ocr_preview": verification.get("ocr_preview", ""),
            "notification_count": sidebar["pending_teachers"]
            + sidebar["pending_student_ids"],
        },
        page_title=f"Review Student — {user.get_full_name() or user.username}",
    )
