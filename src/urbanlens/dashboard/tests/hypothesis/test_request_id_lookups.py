"""A row id taken from a request must not reach `filter(pk=...)` unparsed.

`Model.objects.filter(pk="abc")` raises `ValueError: Field 'id' expected a number
but got 'abc'`, and `pk=""` raises the same - only `pk=None` degrades to a
zero-row `IS NULL` lookup. Every site below already had an "it did not resolve"
branch on the next line, so a malformed id was meant to be a no-op; instead it
was a 500.

Found via P85: mypy reports these as `Incompatible type for lookup 'pk'`, and
`[tool.mypy]`'s `disable_error_code = ['misc']` was switching that off. It only
sees the ones reached through a typed path at all, which is why the two
`userprofile` sites here - reached through a related manager - are not in its
list despite carrying the same defect in a worse form: their `.get(..., "")`
default makes the crash the *default* behaviour of an omitted field.
"""

from __future__ import annotations

from django.contrib.auth.models import User
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.profile.email import ProfileEmail

#: Values a form field can carry that `int()` refuses. "" is what
#: `request.POST.get(name, "")` yields for a field the client omitted, so it is
#: not a hostile input so much as the ordinary one. Note what is *not* here:
#: `int("\u0663")` is 3 and `int(" 5 ")` is 5 - Python accepts any Unicode
#: decimal digit and surrounding space, so neither is a malformed id, and using
#: one as a fixture would silently address whichever row holds that pk.
MALFORMED_IDS = ["", "abc", "12abc", "1;2", "5.0", "0x3"]


class SecondaryEmailIdTests(TestCase):
    """`profile.edit`'s two email actions both look a row up by a posted id."""

    def setUp(self) -> None:
        super().setUp()
        self.user = baker.make(User)
        self.profile = self.user.profile
        self.client.force_login(self.user)
        self.url = reverse("profile.edit")
        self.email = ProfileEmail.objects.create(profile=self.profile, email="second@example.test")

    def test_remove_email_with_a_malformed_id_is_a_no_op(self) -> None:
        for value in MALFORMED_IDS:
            with self.subTest(email_id=value):
                response = self.client.post(
                    self.url, {"action": "remove_email", "email_id": value}, HTTP_HX_REQUEST="true"
                )
                self.assertEqual(response.status_code, 200)
                self.assertTrue(ProfileEmail.objects.filter(pk=self.email.pk).exists(), "the real row must survive")

    def test_remove_email_with_no_id_at_all_is_a_no_op(self) -> None:
        response = self.client.post(self.url, {"action": "remove_email"}, HTTP_HX_REQUEST="true")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(ProfileEmail.objects.filter(pk=self.email.pk).exists())

    def test_remove_email_still_removes_a_real_id(self) -> None:
        response = self.client.post(
            self.url, {"action": "remove_email", "email_id": str(self.email.pk)}, HTTP_HX_REQUEST="true"
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(ProfileEmail.objects.filter(pk=self.email.pk).exists())

    def test_resend_verification_with_a_malformed_id_is_a_no_op(self) -> None:
        for value in MALFORMED_IDS:
            with self.subTest(email_id=value):
                response = self.client.post(
                    self.url,
                    {"action": "resend_email_verification", "email_id": value},
                    HTTP_HX_REQUEST="true",
                )
                self.assertEqual(response.status_code, 200)

    def test_resend_verification_with_no_id_at_all_is_a_no_op(self) -> None:
        response = self.client.post(self.url, {"action": "resend_email_verification"}, HTTP_HX_REQUEST="true")
        self.assertEqual(response.status_code, 200)


class SiteAdminUserIdTests(TestCase):
    """The site-admin user actions take their target id from the posted form."""

    def setUp(self) -> None:
        super().setUp()
        self.admin = baker.make(User, is_staff=True, is_superuser=True)
        self.client.force_login(self.admin)

    def test_user_action_with_a_malformed_id_reports_not_found(self) -> None:
        url = reverse("site_admin_users")
        for value in MALFORMED_IDS:
            with self.subTest(user_id=value):
                response = self.client.post(url, {"user_id": value, "action": "request_delete"})
                self.assertIn(response.status_code, (302, 200), "a malformed id must not 500")

    def test_user_action_with_no_id_at_all_reports_not_found(self) -> None:
        response = self.client.post(reverse("site_admin_users"), {"action": "request_delete"})
        self.assertIn(response.status_code, (302, 200))
