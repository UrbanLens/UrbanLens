"""Guards on admin-initiated account deletion.

`SiteAdminUsersView.post` deletes other people's accounts. The coverage run over the
full suite showed it never executes - so the three things standing between an admin
misclick and someone's data (no self-deletion, no deleting admin accounts, and a typed
confirmation) were unverified.

The handler's class docstring still describes it as a "read-only directory of
registered users", documenting only GET; that is corrected alongside these tests.
"""

from __future__ import annotations

from django.contrib.auth.models import Group, User
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.profile.model import Profile, VisibilityChoice
from urbanlens.dashboard.services.admin.site_admin import SITE_ADMIN_GROUP_NAME


class AdminUserDeletionGuardTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.admin = baker.make(User, username="zzaudit-admin", is_superuser=True)
        Profile.objects.get_or_create(user=self.admin)
        self.client.force_login(self.admin)
        self.target = baker.make(User, username="zzaudit-target")
        self.target_profile = Profile.objects.get(user=self.target)
        # profile_visibility defaults to ANYTHING_IN_COMMON, and a fresh admin shares
        # nothing with a fresh user - so by default the handler treats the target as
        # hidden and expects the literal "hidden user" as confirmation instead of the
        # username. Made visible here so these tests exercise the username branch; the
        # hidden branch has its own test below.
        self.target_profile.profile_visibility = VisibilityChoice.ANYONE
        self.target_profile.save(update_fields=["profile_visibility"])

    def _post(self, **data) -> object:
        return self.client.post(reverse("site_admin_users"), data=data)

    def _pending(self, profile: Profile) -> bool:
        profile.refresh_from_db()
        return profile.deletion_scheduled_for is not None

    def test_a_correct_confirmation_schedules_deletion(self) -> None:
        """The control: without this, every guard test below could pass because
        deletion never works at all."""
        self._post(action="request_delete", user_id=self.target.pk, confirm_text="zzaudit-target")

        self.assertTrue(self._pending(self.target_profile))

    def test_a_wrong_confirmation_does_not_delete(self) -> None:
        self._post(action="request_delete", user_id=self.target.pk, confirm_text="not-the-username")

        self.assertFalse(self._pending(self.target_profile))

    def test_a_missing_confirmation_does_not_delete(self) -> None:
        self._post(action="request_delete", user_id=self.target.pk)

        self.assertFalse(self._pending(self.target_profile))

    def test_an_admin_cannot_delete_their_own_account_here(self) -> None:
        admin_profile = Profile.objects.get(user=self.admin)

        self._post(action="request_delete", user_id=self.admin.pk, confirm_text=self.admin.username)

        self.assertFalse(self._pending(admin_profile))

    def test_a_superuser_account_cannot_be_deleted(self) -> None:
        other_admin = baker.make(User, username="zzaudit-other-admin", is_superuser=True)
        other_profile = Profile.objects.get(user=other_admin)

        self._post(action="request_delete", user_id=other_admin.pk, confirm_text=other_admin.username)

        self.assertFalse(self._pending(other_profile))

    def test_a_site_admin_group_member_cannot_be_deleted(self) -> None:
        group, _ = Group.objects.get_or_create(name=SITE_ADMIN_GROUP_NAME)
        staff = baker.make(User, username="zzaudit-staff")
        staff.groups.add(group)
        staff_profile = Profile.objects.get(user=staff)

        self._post(action="request_delete", user_id=staff.pk, confirm_text=staff.username)

        self.assertFalse(self._pending(staff_profile))

    def test_cancel_restores_the_account(self) -> None:
        self._post(action="request_delete", user_id=self.target.pk, confirm_text="zzaudit-target")
        self.assertTrue(self._pending(self.target_profile))

        self._post(action="cancel_delete", user_id=self.target.pk)

        self.assertFalse(self._pending(self.target_profile))

    def test_a_hidden_user_is_confirmed_with_the_placeholder_not_their_username(self) -> None:
        """The handler avoids echoing a username the admin is not allowed to see, so the
        confirmation string for a hidden profile is the fixed literal "hidden user".
        Worth pinning: it means the typed confirmation is a constant for every user an
        admin cannot see, which by default is most of them."""
        hidden = baker.make(User, username="zzaudit-hidden")
        hidden_profile = Profile.objects.get(user=hidden)

        self._post(action="request_delete", user_id=hidden.pk, confirm_text=hidden.username)
        self.assertFalse(self._pending(hidden_profile), "the real username should not confirm a hidden profile")

        self._post(action="request_delete", user_id=hidden.pk, confirm_text="hidden user")
        self.assertTrue(self._pending(hidden_profile))

    def test_a_non_admin_cannot_reach_the_endpoint(self) -> None:
        outsider = baker.make(User, username="zzaudit-outsider")
        Profile.objects.get_or_create(user=outsider)
        self.client.force_login(outsider)

        response = self._post(action="request_delete", user_id=self.target.pk, confirm_text="zzaudit-target")

        self.assertIn(response.status_code, (302, 403))
        self.assertFalse(self._pending(self.target_profile))
