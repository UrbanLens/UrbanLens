"""Tests for the "view my profile as another user" preview feature.

Covers:
- preview_modes() mirroring the settings-page VisibilityChoice options
- create_ghost_viewer() building the correct relationship per mode
- ProfilePreviewMiddleware end-to-end: simulated rendering, banner injection,
  rollback of all ghost rows, write blocking, and auto-exit on navigation
- The property that each simulated audience sees exactly what the owner's
  profile_visibility setting permits
"""

from __future__ import annotations

from django.contrib.auth.models import User
from django.db import transaction
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.profile.model import Profile, VisibilityChoice
from urbanlens.dashboard.services.profile_preview import SESSION_KEY, create_ghost_viewer, preview_modes


def _owner() -> Profile:
    return baker.make("auth.User").profile


# -- preview_modes ---------------------------------------------------------------


class PreviewModesTests(TestCase):
    """The preview menu must mirror the privacy controls on the settings page."""

    def test_modes_match_visibility_choices(self) -> None:
        self.assertEqual([mode for mode, _ in preview_modes()], list(VisibilityChoice.values))

    def test_every_mode_has_a_label(self) -> None:
        for _mode, label in preview_modes():
            self.assertTrue(label)


# -- create_ghost_viewer ---------------------------------------------------------


class CreateGhostViewerTests(TestCase):
    """Each mode must create a ghost standing in the promised relationship."""

    def setUp(self) -> None:
        super().setUp()
        self.owner = _owner()

    def _ghost_profile(self, mode: str) -> Profile:
        with transaction.atomic():
            ghost_user = create_ghost_viewer(self.owner, mode)
            return Profile.objects.get(user=ghost_user)

    def test_friend_mode_creates_accepted_friendship(self) -> None:
        from urbanlens.dashboard.models.friendship.meta import FriendshipStatus
        from urbanlens.dashboard.models.friendship.model import Friendship

        ghost = self._ghost_profile(VisibilityChoice.FRIENDS)
        friendship = Friendship.objects.all().between(ghost, self.owner)
        self.assertIsNotNone(friendship)
        self.assertEqual(friendship.status, FriendshipStatus.ACCEPTED)

    def test_common_pin_mode_shares_a_location(self) -> None:
        from urbanlens.dashboard.models.pin.model import Pin

        ghost = self._ghost_profile(VisibilityChoice.COMMON_PIN)
        owner_locations = set(Pin.objects.filter(profile=self.owner).values_list("location_id", flat=True))
        ghost_locations = set(Pin.objects.filter(profile=ghost).values_list("location_id", flat=True))
        self.assertTrue(owner_locations & ghost_locations)

    def test_common_friend_mode_shares_a_friend_without_direct_friendship(self) -> None:
        from urbanlens.dashboard.models.friendship.meta import FriendshipStatus
        from urbanlens.dashboard.models.friendship.model import Friendship

        ghost = self._ghost_profile(VisibilityChoice.COMMON_FRIEND)
        self.assertIsNone(Friendship.objects.all().between(ghost, self.owner))

        def friends_of(profile: Profile) -> set[int]:
            accepted = Friendship.objects.filter(status=FriendshipStatus.ACCEPTED)
            return set(accepted.filter(from_profile=profile).values_list("to_profile_id", flat=True)) | set(
                accepted.filter(to_profile=profile).values_list("from_profile_id", flat=True),
            )

        self.assertTrue(friends_of(ghost) & friends_of(self.owner))

    def test_common_trip_mode_shares_a_trip(self) -> None:
        from urbanlens.dashboard.models.trips.model import TripMembership

        ghost = self._ghost_profile(VisibilityChoice.COMMON_TRIP)
        owner_trips = set(TripMembership.objects.filter(profile=self.owner).values_list("trip_id", flat=True))
        ghost_trips = set(TripMembership.objects.filter(profile=ghost).values_list("trip_id", flat=True))
        self.assertTrue(owner_trips & ghost_trips)

    def test_stranger_modes_create_no_relationship(self) -> None:
        from urbanlens.dashboard.models.friendship.model import Friendship
        from urbanlens.dashboard.models.pin.model import Pin
        from urbanlens.dashboard.models.trips.model import TripMembership

        for mode in (VisibilityChoice.ANYONE, VisibilityChoice.NO_ONE):
            ghost = self._ghost_profile(mode)
            self.assertIsNone(Friendship.objects.all().between(ghost, self.owner))
            self.assertFalse(Pin.objects.filter(profile=ghost).exists())
            self.assertFalse(TripMembership.objects.filter(profile=ghost).exists())


# -- End-to-end preview flow -----------------------------------------------------


class ProfilePreviewFlowTests(TestCase):
    """Starting, rendering, and exiting a preview through the full middleware stack."""

    def setUp(self) -> None:
        super().setUp()
        self.owner = _owner()
        self.owner.profile_visibility = VisibilityChoice.ANYONE
        self.owner.save(update_fields=["profile_visibility"])
        self.client.force_login(self.owner.user)
        self.profile_path = reverse("profile.view_user", kwargs={"profile_slug": self.owner.slug})

    def _start_preview(self, mode: str):
        return self.client.post(reverse("profile.preview", args=[mode]))

    def test_own_profile_shows_view_as_menu(self) -> None:
        response = self.client.get(reverse("profile.view"))
        self.assertContains(response, "View as")
        for mode, label in preview_modes():
            self.assertContains(response, reverse("profile.preview", args=[mode]))
            self.assertContains(response, label)

    def test_start_preview_sets_session_and_redirects_to_public_page(self) -> None:
        response = self._start_preview(VisibilityChoice.FRIENDS)
        self.assertRedirects(response, self.profile_path, fetch_redirect_response=False)
        state = self.client.session[SESSION_KEY]
        self.assertEqual(state["mode"], VisibilityChoice.FRIENDS)
        self.assertEqual(state["path"], self.profile_path)
        self.assertEqual(state["owner_id"], self.owner.pk)

    def test_unknown_mode_is_rejected(self) -> None:
        response = self._start_preview("not_a_mode")
        self.assertRedirects(response, reverse("profile.view"), fetch_redirect_response=False)
        self.assertNotIn(SESSION_KEY, self.client.session)

    def test_previewed_page_renders_as_other_user_with_banner(self) -> None:
        import re

        self._start_preview(VisibilityChoice.ANYONE)
        response = self.client.get(self.profile_path)
        self.assertEqual(response.status_code, 200)
        # The exact page another user gets: no owner-only controls. Checked
        # against script-stripped content - the bio editor's wiring script
        # contains "Edit Profile" as an inert JS comment on every render,
        # which is not the owner-only BUTTON this assertion is about.
        content = re.sub(r"<script\b[^>]*>.*?</script>", "", response.content.decode(), flags=re.DOTALL)
        self.assertNotIn("Edit Profile", content)
        self.assertIn("Add Friend", content)
        # ...plus the injected preview banner.
        self.assertIn("profile-preview-banner", content)
        self.assertIn(reverse("profile.preview.exit"), content)

    def test_ghost_rows_are_rolled_back(self) -> None:
        self._start_preview(VisibilityChoice.COMMON_TRIP)
        users_before = User.objects.count()
        self.client.get(self.profile_path)
        self.assertEqual(User.objects.count(), users_before)
        self.assertFalse(User.objects.filter(username__startswith="preview_").exists())

    def test_writes_are_blocked_during_preview(self) -> None:
        self._start_preview(VisibilityChoice.ANYONE)
        response = self.client.post(self.profile_path)
        self.assertEqual(response.status_code, 403)

    def test_htmx_fragment_from_previewed_page_is_simulated(self) -> None:
        self._start_preview(VisibilityChoice.ANYONE)
        response = self.client.get(
            reverse("friend.list", args=[self.owner.pk]),
            HTTP_HX_REQUEST="true",
            HTTP_REFERER=f"http://testserver{self.profile_path}",
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(User.objects.filter(username__startswith="preview_").exists())

    def test_navigating_away_exits_preview(self) -> None:
        self._start_preview(VisibilityChoice.ANYONE)
        self.client.get(reverse("profile.edit"), HTTP_ACCEPT="text/html")
        self.assertNotIn(SESSION_KEY, self.client.session)

    def test_exit_view_clears_preview(self) -> None:
        self._start_preview(VisibilityChoice.ANYONE)
        response = self.client.get(reverse("profile.preview.exit"))
        self.assertRedirects(response, reverse("profile.view"), fetch_redirect_response=False)
        self.assertNotIn(SESSION_KEY, self.client.session)


# -- Visibility property ---------------------------------------------------------


class PreviewVisibilityPropertyTests(TestCase):
    """A simulated audience must see exactly what the privacy setting permits."""

    # What relationship create_ghost_viewer() actually establishes for each mode.
    # ANYTHING_IN_COMMON only fabricates a shared pin (the lightest relation to
    # fake - see profile_preview._share_a_pin), so it carries the same "pin" tag
    # as COMMON_PIN rather than a tag of its own.
    _MODE_RELATIONS: dict[str, frozenset[str]] = {
        VisibilityChoice.ANYONE: frozenset(),
        VisibilityChoice.NO_ONE: frozenset(),
        VisibilityChoice.FRIENDS: frozenset({"friends"}),
        VisibilityChoice.COMMON_PIN: frozenset({"pin"}),
        VisibilityChoice.COMMON_FRIEND: frozenset({"friend"}),
        VisibilityChoice.COMMON_TRIP: frozenset({"trip"}),
        VisibilityChoice.ANYTHING_IN_COMMON: frozenset({"pin"}),
    }

    def setUp(self) -> None:
        super().setUp()
        self.owner = _owner()
        self.client.force_login(self.owner.user)
        self.profile_path = reverse("profile.view_user", kwargs={"profile_slug": self.owner.slug})

    def _expected_visible(self, mode: str, visibility: str) -> bool:
        """Mirror Profile.visibility_permits() given the relation create_ghost_viewer() sets up."""
        if visibility == VisibilityChoice.ANYONE:
            return True
        if visibility == VisibilityChoice.NO_ONE:
            return False
        relations = self._MODE_RELATIONS[mode]
        if "friends" in relations:
            return True
        if visibility == VisibilityChoice.FRIENDS:
            return False
        if visibility == VisibilityChoice.COMMON_PIN:
            return "pin" in relations
        if visibility == VisibilityChoice.COMMON_FRIEND:
            return "friend" in relations
        if visibility == VisibilityChoice.COMMON_TRIP:
            return "trip" in relations
        if visibility == VisibilityChoice.ANYTHING_IN_COMMON:
            return bool(relations & {"pin", "friend", "trip"})
        return False

    def test_preview_matches_profile_visibility(self) -> None:
        """The previewed page is visible iff the real audience would pass the check.

        Exhaustive over every (simulated audience, profile_visibility) pair: a
        ghost sees the profile when it is visible to any logged-in user, or
        when the ghost's relationship satisfies Profile.visibility_permits().
        """
        for mode, _label in preview_modes():
            for visibility in VisibilityChoice.values:
                with self.subTest(mode=mode, visibility=visibility):
                    Profile.objects.filter(pk=self.owner.pk).update(profile_visibility=visibility)
                    self.client.post(reverse("profile.preview", args=[mode]))
                    response = self.client.get(self.profile_path)

                    expected_visible = self._expected_visible(mode, visibility)
                    self.assertEqual(response.status_code, 200 if expected_visible else 404)
                    self.assertFalse(User.objects.filter(username__startswith="preview_").exists())
