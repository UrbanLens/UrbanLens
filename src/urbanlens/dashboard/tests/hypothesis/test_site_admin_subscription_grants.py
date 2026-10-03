"""Site admin > Subscriptions' grant list and revoke: every site admin manages every grant, nobody else any (P199)."""

from __future__ import annotations

from django.contrib.auth.models import Group, User
from django.test import Client
from django.urls import reverse
from django.utils import timezone
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.models.subscriptions import SubscriptionRole, UserSubscription, grant_subscription
from urbanlens.dashboard.services.admin.site_admin import SITE_ADMIN_GROUP_NAME, add_user_to_site_admin_group

_URL = reverse("site_admin_subscriptions")


def _admin(username: str) -> User:
    user = baker.make(User, username=username)
    add_user_to_site_admin_group(user)
    Profile.objects.filter(user=user).update(welcome_onboarding_complete=True, profile_setup_complete=True)
    return user


def _client_for(user: User | None, *, enforce_csrf_checks: bool = False) -> Client:
    client = Client(enforce_csrf_checks=enforce_csrf_checks)
    if user is not None:
        client.force_login(user)
    return client


class _GrantFixture(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.admin_a = _admin("admin-alpha")
        self.admin_b = _admin("admin-bravo")
        self.role = baker.make(SubscriptionRole, slug="explorer", name="Explorer", features="")
        self.grantee = baker.make(User, username="grantee-gina", email="gina@example.com")
        self.grant = grant_subscription(self.grantee, self.role, self.admin_a, None)

    def _revoke(self, client: Client, grant: UserSubscription | None = None, **extra):
        return client.post(_URL, {"action": "revoke", "subscription_id": (grant or self.grant).pk}, **extra)

    def _assert_untouched(self) -> None:
        self.grant.refresh_from_db()
        self.assertIsNone(self.grant.revoked_at)
        self.assertIsNone(self.grant.expires_at)


class OutsiderRefusalTests(_GrantFixture):
    """Exploit attempts: none may read the grant list or change a grant."""

    def setUp(self) -> None:
        super().setUp()
        self.member = baker.make(User, username="plain-member")
        Profile.objects.filter(user=self.member).update(welcome_onboarding_complete=True, profile_setup_complete=True)

    def test_a_member_cannot_list_grants(self) -> None:
        response = _client_for(self.member).get(_URL)

        self.assertEqual(response.status_code, 403)
        self.assertNotContains(response, "grantee-gina", status_code=403)

    def test_a_member_cannot_revoke_someone_elses_grant(self) -> None:
        response = self._revoke(_client_for(self.member))

        self.assertEqual(response.status_code, 403)
        self._assert_untouched()

    def test_a_member_cannot_revoke_through_htmx(self) -> None:
        response = self._revoke(_client_for(self.member), HTTP_HX_REQUEST="true")

        self.assertEqual(response.status_code, 403)
        self.assertNotContains(response, "grantee-gina", status_code=403)
        self._assert_untouched()

    def test_a_member_cannot_change_a_grants_duration(self) -> None:
        response = _client_for(self.member).post(
            _URL, {"action": "update", "subscription_id": self.grant.pk, "duration_months": "1"}
        )

        self.assertEqual(response.status_code, 403)
        self._assert_untouched()

    def test_a_member_who_is_the_grantee_cannot_revoke_their_own_grant(self) -> None:
        response = self._revoke(_client_for(self.grantee))

        self.assertEqual(response.status_code, 403)
        self._assert_untouched()

    def test_a_staff_flag_without_the_site_admin_permission_is_refused(self) -> None:
        self.member.is_staff = True
        self.member.save(update_fields=["is_staff"])

        response = self._revoke(_client_for(self.member))

        self.assertEqual(response.status_code, 403)
        self._assert_untouched()

    def test_an_admin_who_lost_the_role_is_refused(self) -> None:
        self.admin_a.groups.remove(Group.objects.get(name=SITE_ADMIN_GROUP_NAME))

        self.assertEqual(_client_for(self.admin_a).get(_URL).status_code, 403)
        self.assertEqual(self._revoke(_client_for(self.admin_a)).status_code, 403)
        self._assert_untouched()

    def test_an_anonymous_visitor_cannot_list_grants(self) -> None:
        response = _client_for(None).get(_URL)

        self.assertIn(response.status_code, (302, 403))
        self.assertNotIn(b"grantee-gina", response.content)

    def test_an_anonymous_visitor_cannot_revoke(self) -> None:
        response = self._revoke(_client_for(None))

        self.assertIn(response.status_code, (302, 403))
        self._assert_untouched()


class RevokeRequestShapeTests(_GrantFixture):
    def test_revoke_without_a_csrf_token_is_refused(self) -> None:
        response = self._revoke(_client_for(self.admin_b, enforce_csrf_checks=True))

        self.assertEqual(response.status_code, 403)
        self._assert_untouched()

    def test_a_get_carrying_revoke_parameters_changes_nothing(self) -> None:
        response = _client_for(self.admin_b).get(_URL, {"action": "revoke", "subscription_id": self.grant.pk})

        self.assertEqual(response.status_code, 200)
        self._assert_untouched()

    def test_the_list_renders_every_revoke_form_with_a_csrf_token(self) -> None:
        html = _client_for(self.admin_b).get(_URL).content.decode()

        form_start = html.index(f'name="subscription_id" value="{self.grant.pk}"')
        form_open = html.rindex("<form", 0, form_start)
        self.assertIn('method="post"', html[form_open:form_start])
        self.assertIn("csrfmiddlewaretoken", html[form_open:form_start])

    def test_revoking_twice_keeps_the_first_revocation(self) -> None:
        client = _client_for(self.admin_b)
        self._revoke(client)
        self.grant.refresh_from_db()
        first = self.grant.revoked_at
        assert first is not None

        response = self._revoke(client)

        self.assertEqual(response.status_code, 302)
        self.grant.refresh_from_db()
        self.assertEqual(self.grant.revoked_at, first)

    def test_a_revoke_is_logged_with_who_revoked(self) -> None:
        with self.assertLogs("urbanlens.dashboard.controllers.site_admin", level="INFO") as logs:
            self._revoke(_client_for(self.admin_b))

        line = next(record for record in logs.records if "revoked" in record.getMessage())
        self.assertIn(str(self.grant.pk), line.getMessage())
        self.assertIn(str(self.admin_b.pk), line.getMessage())


class EveryAdminManagesEveryGrantTests(_GrantFixture):
    def test_another_admin_sees_the_grant_and_who_made_it(self) -> None:
        response = _client_for(self.admin_b).get(_URL)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(list(response.context["grants"]), [self.grant])
        self.assertContains(response, "grantee-gina")
        self.assertContains(response, "admin-alpha")
        self.assertContains(response, timezone.localtime(self.grant.created).strftime("%b %-d, %Y"))

    def test_another_admin_revokes_the_grant(self) -> None:
        response = self._revoke(_client_for(self.admin_b))

        self.assertEqual(response.status_code, 302)
        self.grant.refresh_from_db()
        self.assertIsNotNone(self.grant.revoked_at)

    def test_another_admin_changes_the_grants_duration(self) -> None:
        _client_for(self.admin_b).post(
            _URL, {"action": "update", "subscription_id": self.grant.pk, "duration_months": "3"}
        )

        self.grant.refresh_from_db()
        self.assertIsNotNone(self.grant.expires_at)

    def test_the_htmx_revoke_rerenders_every_admins_remaining_grants(self) -> None:
        other_grantee = baker.make(User, username="grantee-otto")
        other = grant_subscription(other_grantee, self.role, self.admin_a, None)

        response = self._revoke(_client_for(self.admin_b), HTTP_HX_REQUEST="true")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(list(response.context["grants"]), [other])
        self.assertContains(response, "grantee-otto")
        self.assertNotContains(response, "grantee-gina")

    def test_a_grant_by_an_admin_who_lost_the_role_stays_manageable(self) -> None:
        self.admin_a.groups.remove(Group.objects.get(name=SITE_ADMIN_GROUP_NAME))
        client = _client_for(self.admin_b)

        self.assertEqual(list(client.get(_URL).context["grants"]), [self.grant])
        self._revoke(client)

        self.grant.refresh_from_db()
        self.assertIsNotNone(self.grant.revoked_at)

    def test_a_grant_by_a_deactivated_admin_stays_manageable(self) -> None:
        self.admin_a.is_active = False
        self.admin_a.save(update_fields=["is_active"])
        client = _client_for(self.admin_b)

        response = client.get(_URL)
        self.assertEqual(list(response.context["grants"]), [self.grant])
        self.assertContains(response, "admin-alpha")
        self._revoke(client)

        self.grant.refresh_from_db()
        self.assertIsNotNone(self.grant.revoked_at)

    def test_revoked_grants_are_not_listed(self) -> None:
        self.grant.revoked_at = timezone.now()
        self.grant.save(update_fields=["revoked_at"])

        self.assertEqual(list(_client_for(self.admin_b).get(_URL).context["grants"]), [])
