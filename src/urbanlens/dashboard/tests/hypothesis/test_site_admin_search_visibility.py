"""The admin directory's search box must not be a stronger oracle than the row it returns."""

from __future__ import annotations

from django.contrib.auth.models import User
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.friendship.meta import FriendshipStatus
from urbanlens.dashboard.models.friendship.model import Friendship
from urbanlens.dashboard.models.profile.meta import VisibilityChoice
from urbanlens.dashboard.models.profile.model import Profile


class AdminSearchVisibilityTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # absorbs the bootstrap site-admin promotion
        self.admin = baker.make(User, username="zzsearch-admin", is_superuser=True)
        Profile.objects.get_or_create(user=self.admin)
        self.client.force_login(self.admin)

    def _make(self, *, username: str, email: str, first_name: str, **visibility) -> User:
        user = baker.make(User, username=username, email=email, first_name=first_name)
        profile, _ = Profile.objects.get_or_create(user=user)
        if visibility:
            Profile.objects.filter(pk=profile.pk).update(**visibility)
        return user

    def _search(self, term: str) -> set[str]:
        """The usernames of the rows a search returns, which is the oracle under test."""
        response = self.client.get(reverse("site_admin_users"), {"q": term})
        self.assertEqual(response.status_code, 200)
        return {row["user"].username for row in response.context["rows"]}

    def _befriend(self, other: User) -> None:
        baker.make(
            Friendship,
            from_profile=self.admin.profile,
            to_profile=other.profile,
            status=FriendshipStatus.ACCEPTED,
        )

    # --- email, against contact_visibility ---------------------------------

    def test_a_visible_email_is_searchable(self) -> None:
        """The control. Without it, every email test below passes on a broken search."""
        user = self._make(
            username="zzsearch-open",
            email="open-address@example.test",
            first_name="Openly",
            contact_visibility=VisibilityChoice.ANYONE,
        )

        self.assertEqual(self._search("open-address@example.test"), {user.username})

    def test_a_hidden_email_cannot_be_confirmed_by_searching_for_it(self) -> None:
        self._make(
            username="zzsearch-hidden-mail",
            email="secret-address@example.test",
            first_name="Quiet",
            contact_visibility=VisibilityChoice.NO_ONE,
            profile_visibility=VisibilityChoice.NO_ONE,
        )

        self.assertEqual(self._search("secret-address@example.test"), set())

    def test_a_friends_only_email_is_searchable_by_their_friend(self) -> None:
        """Masking is per viewer, not global: an admin who *does* pass the gate keeps the search."""
        user = self._make(
            username="zzsearch-friend",
            email="friend-address@example.test",
            first_name="Palsy",
            contact_visibility=VisibilityChoice.FRIENDS,
            profile_visibility=VisibilityChoice.FRIENDS,
        )
        self._befriend(user)

        self.assertEqual(self._search("friend-address@example.test"), {user.username})

    def test_a_friends_only_email_is_not_searchable_by_a_stranger(self) -> None:
        self._make(
            username="zzsearch-stranger",
            email="stranger-address@example.test",
            first_name="Aloof",
            contact_visibility=VisibilityChoice.FRIENDS,
            profile_visibility=VisibilityChoice.NO_ONE,
        )

        self.assertEqual(self._search("stranger-address@example.test"), set())

    # --- username and first name, against profile_visibility ---------------

    def test_a_visible_username_is_searchable(self) -> None:
        user = self._make(
            username="zzsearch-loud",
            email="loud@example.test",
            first_name="Loud",
            profile_visibility=VisibilityChoice.ANYONE,
        )

        self.assertEqual(self._search("zzsearch-loud"), {user.username})

    def test_a_hidden_username_cannot_be_confirmed_by_searching_for_it(self) -> None:
        """The row renders "Invisible User", so the username is a masked field too."""
        self._make(
            username="zzsearch-ghostname",
            email="ghost@example.test",
            first_name="Ghosty",
            profile_visibility=VisibilityChoice.NO_ONE,
            contact_visibility=VisibilityChoice.NO_ONE,
        )

        self.assertEqual(self._search("zzsearch-ghostname"), set())

    def test_a_hidden_first_name_cannot_be_confirmed_by_searching_for_it(self) -> None:
        self._make(
            username="zzsearch-noname",
            email="noname@example.test",
            first_name="Rumpelstiltskin",
            profile_visibility=VisibilityChoice.NO_ONE,
            contact_visibility=VisibilityChoice.NO_ONE,
        )

        self.assertEqual(self._search("Rumpelstiltskin"), set())

    def test_a_visible_first_name_is_searchable(self) -> None:
        user = self._make(
            username="zzsearch-named",
            email="named@example.test",
            first_name="Rumpelstiltskin",
            profile_visibility=VisibilityChoice.ANYONE,
        )

        self.assertEqual(self._search("Rumpelstiltskin"), {user.username})

    # --- the fields are gated independently --------------------------------

    def test_a_hidden_email_does_not_hide_a_visible_username(self) -> None:
        """Per-field, not per-row: one masked field must not remove the whole account
        from a directory an admin needs in order to manage quotas and deletions."""
        user = self._make(
            username="zzsearch-mixed",
            email="mixed-address@example.test",
            first_name="Mixed",
            profile_visibility=VisibilityChoice.ANYONE,
            contact_visibility=VisibilityChoice.NO_ONE,
        )

        self.assertEqual(self._search("zzsearch-mixed"), {user.username})
        self.assertEqual(self._search("mixed-address@example.test"), set())

    def test_an_admin_can_always_find_their_own_account(self) -> None:
        """``can_view_*`` short-circuits on self before consulting any setting."""
        Profile.objects.filter(user=self.admin).update(
            profile_visibility=VisibilityChoice.NO_ONE,
            contact_visibility=VisibilityChoice.NO_ONE,
        )
        User.objects.filter(pk=self.admin.pk).update(email="admin-own@example.test")

        self.assertEqual(self._search("admin-own@example.test"), {self.admin.username})
        self.assertEqual(self._search("zzsearch-admin"), {self.admin.username})

    def test_the_unsearched_listing_still_shows_hidden_accounts(self) -> None:
        """The fix restricts *matching*, not membership - an admin browsing the
        directory must still see that a hidden account exists."""
        user = self._make(
            username="zzsearch-listed",
            email="listed@example.test",
            first_name="Listed",
            profile_visibility=VisibilityChoice.NO_ONE,
            contact_visibility=VisibilityChoice.NO_ONE,
        )

        response = self.client.get(reverse("site_admin_users"))
        self.assertEqual(response.status_code, 200)
        usernames = {row["user"].username for row in response.context["rows"]}

        self.assertIn(user.username, usernames)
