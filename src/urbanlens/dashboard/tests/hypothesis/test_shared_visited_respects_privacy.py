""" "Visited Together" must obey the same opt-in as "Places in Common".

``_add_common_context`` gates ``common_pin_count`` behind
``Profile.can_view_common_pins_with`` - deliberately mutual, because, as that
method's docstring puts it, "revealing which locations a pair of users have both
pinned exposes information about *both* of them, not just this profile". The
comment above the gate in the controller spells out that the *count* had to be
gated too, not just the link to the detail page.

``shared_visited`` was computed and put in the context unconditionally, and the
profile template renders it as a "Visited Together" stat whenever it is non-empty.
So a profile whose ``common_pins_visibility`` forbids this viewer (or a viewer
whose own setting forbids it - the check is mutual) still discloses how many
locations the two have both *visited*.

That is strictly more than the thing deliberately protected: a shared pin means
two people bookmarked a place, while a shared visit means both were physically
there. There is no separate visits-visibility setting, so the common-pins gate is
the applicable one - the same class of "what we have in common" disclosure.
"""

from __future__ import annotations

from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.labels import ensure_label
from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.labels.meta import KIND_STATUS
from urbanlens.dashboard.models.labels.model import Label
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.profile.meta import VisibilityChoice
from urbanlens.dashboard.models.profile.model import Profile


class SharedVisitedPrivacyTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.viewer_user = baker.make("auth.User")
        self.viewer = Profile.objects.get(user=self.viewer_user)
        self.subject = Profile.objects.get(user=baker.make("auth.User"))

        # One location both have pinned and both have marked visited.
        self.location = baker.make(Location)
        for profile in (self.viewer, self.subject):
            pin = baker.make(Pin, profile=profile, location=self.location)
            pin.labels.add(ensure_label(profile=profile, kind=KIND_STATUS, name="Visited", is_protected=True))

        self.client.force_login(self.viewer_user)

    def _context(self):
        response = self.client.get(reverse("profile.view_user", kwargs={"profile_slug": self.subject.slug}))
        self.assertEqual(response.status_code, 200)
        return response.context

    def _allow_both(self) -> None:
        Profile.objects.filter(pk__in=[self.viewer.pk, self.subject.pk]).update(
            common_pins_visibility=VisibilityChoice.ANYONE
        )

    def test_it_is_shown_when_both_sides_permit_it(self) -> None:
        """Anchors the rest: without this, a passing privacy test could be vacuous."""
        self._allow_both()

        self.assertTrue(self._context()["shared_visited"], "the shared visit should be visible when both opted in")

    def test_it_is_hidden_when_the_subject_forbids_it(self) -> None:
        self._allow_both()
        Profile.objects.filter(pk=self.subject.pk).update(common_pins_visibility=VisibilityChoice.NO_ONE)

        self.assertFalse(self._context()["shared_visited"], "a subject who opted out still disclosed shared visits")

    def test_it_is_hidden_when_the_viewer_forbids_it(self) -> None:
        """The gate is mutual - one side's setting cannot be overridden by the other's."""
        self._allow_both()
        Profile.objects.filter(pk=self.viewer.pk).update(common_pins_visibility=VisibilityChoice.NO_ONE)

        self.assertFalse(self._context()["shared_visited"], "a viewer who opted out still saw shared visits")

    def test_it_tracks_the_same_flag_the_count_uses(self) -> None:
        """The two stats sit side by side; they must not disagree."""
        self._allow_both()
        Profile.objects.filter(pk=self.subject.pk).update(common_pins_visibility=VisibilityChoice.NO_ONE)
        context = self._context()

        self.assertIsNone(context["common_pin_count"])
        self.assertFalse(context["shared_visited"])
