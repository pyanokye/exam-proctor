"""Regression tests: standalone auth pages must never render inside the portal.

A sidebar tab loads with hx-get into #portal-main. Without HX-Redirect the login
document would be swapped in next to a stale portal shell.
"""

from __future__ import annotations

from django.test import Client, TestCase
from django.urls import reverse

from apps.accounts.models import TeacherProfile, User

HTMX = {"HTTP_HX_REQUEST": "true"}


class HtmxAuthRedirectTests(TestCase):
    def setUp(self):
        self.client = Client(SERVER_NAME="localhost")

    def test_expired_session_tab_load_returns_hx_redirect(self):
        response = self.client.get(reverse("accounts:dashboard"), **HTMX)
        self.assertEqual(response.status_code, 204)
        self.assertEqual(
            response.headers["HX-Redirect"], "/login/?next=/dashboard/"
        )
        self.assertNotIn(b"Sign in to your portal", response.content)

    def test_expired_session_tab_load_never_yields_login_markup(self):
        response = self.client.get(
            reverse("accounts:dashboard"), follow=True, **HTMX
        )
        self.assertEqual(response.redirect_chain, [])
        self.assertNotIn(b"Sign in to your portal", response.content)

    def test_htmx_hit_on_login_page_redirects_instead_of_rendering(self):
        response = self.client.get(reverse("accounts:login"), **HTMX)
        self.assertEqual(response.status_code, 204)
        self.assertEqual(response.headers["HX-Redirect"], "/login/")

    def test_full_page_request_still_renders_login(self):
        response = self.client.get(reverse("accounts:dashboard"), follow=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Sign in to your portal", response.content)

    def test_unapproved_teacher_tab_load_returns_hx_redirect(self):
        user = User.objects.create_user(
            username="teach",
            email="teach@example.com",
            password="pw",
            role=User.Role.TEACHER,
        )
        TeacherProfile.objects.create(
            user=user,
            staff_id_number="STF-HTMX",
            approval_status=TeacherProfile.ApprovalStatus.PENDING,
        )
        self.client.force_login(user)
        response = self.client.get(reverse("accounts:dashboard"), **HTMX)
        self.assertEqual(response.status_code, 204)
        self.assertEqual(response.headers["HX-Redirect"], "/pending-approval/")

    def test_authenticated_tab_load_is_unaffected(self):
        self.client.force_login(_admin())
        response = self.client.get(reverse("accounts:dashboard"), **HTMX)
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("HX-Redirect", response.headers)

    def test_portal_pages_are_not_stored_by_the_browser(self):
        self.client.force_login(_admin())
        response = self.client.get(reverse("accounts:dashboard"))
        self.assertIn("no-store", response.headers["Cache-Control"])


class CsrfRecoveryTests(TestCase):
    """A page left open across a login/logout holds a rotated-away token.

    Its next POST must land somewhere usable instead of Django's raw 403 page.
    """

    def setUp(self):
        self.client = Client(SERVER_NAME="localhost", enforce_csrf_checks=True)

    def test_stale_logout_post_sends_anonymous_user_to_login(self):
        response = self.client.post(reverse("accounts:logout"))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"], reverse("accounts:login"))
        self.assertEqual(
            [str(m) for m in response.wsgi_request._messages],
            ["Your session expired. Please sign in again."],
        )

    def test_stale_post_sends_signed_in_user_back_to_dashboard(self):
        self.client.force_login(_admin())
        response = self.client.post(reverse("accounts:user_revoke", args=[1]))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"], reverse("accounts:dashboard"))

    def test_stale_login_form_post_returns_to_the_same_form(self):
        url = reverse("accounts:login_student")
        response = self.client.post(url, {"username": "x", "password": "y"})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"], url)

    def test_stale_htmx_post_gets_hx_redirect(self):
        response = self.client.post(reverse("accounts:logout"), **HTMX)
        self.assertEqual(response.status_code, 204)
        self.assertEqual(response.headers["HX-Redirect"], reverse("accounts:login"))


def _admin():
    return User.objects.create_user(
        username="boss",
        email="boss@example.com",
        password="pw",
        role=User.Role.ADMIN,
    )
