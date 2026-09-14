from urllib.parse import urlparse

from django.contrib import messages
from django.shortcuts import redirect
from django.urls import reverse

from .models import StudentProfile, User
from .portal import hx_redirect


COMMON_EXEMPT_PREFIXES = (
    "/login/",
    "/register/",
    "/logout/",
    "/admin/",
    "/static/",
    # /media/ requests are themselves authenticated + role-gated by the
    # serve_media view in apps/accounts/media.py. They must bypass these
    # status-gate middlewares so that users still on a pending/review page
    # can load their own files (e.g. the ID image they uploaded).
    "/media/",
)


class TeacherApprovalMiddleware:
    """Block unapproved teachers from non-auth pages."""

    EXEMPT_PREFIXES = COMMON_EXEMPT_PREFIXES + ("/pending-approval/",)

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.user.is_authenticated and request.user.role == User.Role.TEACHER:
            path = request.path
            if not any(path.startswith(p) for p in self.EXEMPT_PREFIXES):
                profile = getattr(request.user, "teacher_profile", None)
                if profile and profile.approval_status != profile.ApprovalStatus.APPROVED:
                    pending_url = reverse("accounts:pending_approval")
                    if path != pending_url:
                        return redirect("accounts:pending_approval")
        return self.get_response(request)


class StudentIDReviewMiddleware:
    """Block students with pending/rejected ID reviews from regular pages."""

    EXEMPT_PREFIXES = COMMON_EXEMPT_PREFIXES + ("/id-review/",)

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.user.is_authenticated and request.user.role == User.Role.STUDENT:
            path = request.path
            if not any(path.startswith(p) for p in self.EXEMPT_PREFIXES):
                profile = getattr(request.user, "student_profile", None)
                if (
                    profile
                    and profile.id_review_status
                    != StudentProfile.IDReviewStatus.APPROVED
                ):
                    review_url = reverse("accounts:id_review_pending")
                    if path != review_url:
                        return redirect("accounts:id_review_pending")
        return self.get_response(request)


# Pages that render their own full document (they extend base.html, not the
# portal layout) and therefore must never be swapped into #portal-main.
STANDALONE_AUTH_PREFIXES = (
    "/login/",
    "/register/",
    "/logout/",
    "/pending-approval/",
    "/id-review/",
)


def _is_standalone_auth_path(path):
    return any(path.startswith(prefix) for prefix in STANDALONE_AUTH_PREFIXES)


class HtmxAuthRedirectMiddleware:
    """Turn auth bounces on HTMX requests into full-page browser navigations.

    A sidebar tab is loaded with hx-get into #portal-main. When the session has
    expired the view answers 302 -> /login/, which XHR follows transparently, so
    HTMX receives the login document and swaps it inside the still-rendered
    portal shell. Answering with HX-Redirect instead makes the browser leave the
    portal for the real login page.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        if request.headers.get("HX-Request") != "true":
            return response

        if 300 <= response.status_code < 400:
            location = response.headers.get("Location", "")
            if location and _is_standalone_auth_path(urlparse(location).path):
                self._note_expired_session(request, location)
                return hx_redirect(location)
            return response

        # The redirect may already have been followed by the XHR layer (or a
        # stale tab may target an auth page directly): either way the body is a
        # standalone document, not a portal fragment.
        if response.status_code == 200 and _is_standalone_auth_path(request.path):
            return hx_redirect(request.get_full_path())

        return response

    @staticmethod
    def _note_expired_session(request, location):
        if request.user.is_authenticated:
            return
        if request.path.startswith("/logout/"):
            return
        if not urlparse(location).path.startswith("/login/"):
            return
        messages.add_message(
            request,
            messages.INFO,
            "Your session expired. Please sign in again.",
            fail_silently=True,
        )
