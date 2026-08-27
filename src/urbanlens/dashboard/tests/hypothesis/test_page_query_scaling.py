"""Hot pages and endpoints must cost the same queries whatever the user's data size.

The Organize page rendered `{% if not label.profile %}` several times per label
card to decide whether to show the "global" chip. Accessing ``.profile`` fetches
the owning row, so each occurrence cost one query per label: 134 queries for a
profile with 5 pins and 244 for one with 60. ``label.profile_id`` answers the
same question for free, which is what ``Label.is_global`` now reads - 38 queries
either way.

The assertion is that the count does not *grow* with the data, not that it equals
some number. A fixed expected count would break on unrelated changes and teach
people to bump the constant; growth is the actual defect.

**Measurement trap**: the first user created in a fresh database is auto-promoted
to site admin (``services.admin.site_admin``), and the admin's page renders
differently - fewer queries, as it happens. Comparing a first user against a later
one measures that promotion, not the data size. These tests burn a throwaway user
in ``setUp`` so every measured profile is an ordinary one.
"""

from __future__ import annotations

from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext
from model_bakery import baker

from urbanlens.core.tests.features import grant_alpha_features
from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.labels.meta import KIND_TAG
from urbanlens.dashboard.models.labels.model import Label
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.profile.model import Profile

_SMALL = 4
_LARGE = 32
#: Allows for genuinely per-page variation (e.g. an extra count query when a
#: section becomes non-empty) while still failing on anything per-row.
_TOLERANCE = 4


class PageQueryScalingTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        # Burn the auto-promoted first user; see the module docstring.
        baker.make("auth.User")

    def _user_with_pins(self, count: int):
        user = baker.make("auth.User")
        # The SpotGuessr pin list is behind SiteFeature.ALPHA_FEATURES; the
        # grant is constant-cost, so it cannot disturb any growth assertion.
        grant_alpha_features(user)
        profile = Profile.objects.get(user=user)
        for index in range(count):
            pin = baker.make(Pin, profile=profile, location=baker.make(Location))
            pin.labels.add(baker.make(Label, profile=profile, kind=KIND_TAG, name=f"tag-{index}"))
        return user

    def _queries_for(self, path: str, pin_count: int) -> int:
        client = Client()
        client.force_login(self._user_with_pins(pin_count))
        client.get(path)  # warm anything cached per-process
        with CaptureQueriesContext(connection) as captured:
            response = client.get(path)
        self.assertEqual(response.status_code, 200, f"{path} did not render")
        return len(captured.captured_queries)

    def _assert_flat(self, path: str) -> None:
        small = self._queries_for(path, _SMALL)
        large = self._queries_for(path, _LARGE)

        self.assertLessEqual(
            large - small,
            _TOLERANCE,
            f"{path} scales with pin count: {small} queries for {_SMALL} pins, {large} for {_LARGE}",
        )

    def test_the_organize_page_does_not_scale_with_label_count(self) -> None:
        self._assert_flat("/dashboard/organize/")

    def test_the_map_page_does_not_scale_with_pin_count(self) -> None:
        self._assert_flat("/dashboard/map/")

    def test_the_spotguessr_pin_list_does_not_scale_with_pin_count(self) -> None:
        """This one served every pin's label, which falls through to
        ``Location.display_name`` and reads the reverse OneToOne ``wiki`` - one
        query per pin until ``location__wiki`` was joined in. It was the only
        growing page out of 98 swept."""
        self._assert_flat("/dashboard/spotguessr/pins/")

    def test_is_global_needs_no_query(self) -> None:
        """The mechanism: reading it must not fetch the owning profile."""
        profile = Profile.objects.get(user=baker.make("auth.User"))
        owned = Label.objects.get(pk=baker.make(Label, profile=profile, kind=KIND_TAG, name="mine").pk)
        globally = Label.objects.get(pk=baker.make(Label, profile=None, kind=KIND_TAG, name="shared").pk)

        with CaptureQueriesContext(connection) as captured:
            self.assertFalse(owned.is_global)
            self.assertTrue(globally.is_global)

        self.assertEqual(len(captured.captured_queries), 0, "is_global fetched the profile row")
