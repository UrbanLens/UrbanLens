"""The gate resolved the viewer's whole social graph before asking whether it mattered.

H44's first cut. `visible_to` loaded the viewer's friends, pinned locations and
trip memberships up front - four queries - and only then evaluated the settings
that decide which, if any, of the three the answer depends on. `ANYONE` needs
none of them. `FRIENDS` needs one. `ANYTHING_IN_COMMON` stops at the first
match, so it usually needs fewer than all three.

That is cheap on a gallery listing, where the cost is amortised over many rows.
It is not cheap on the media path, where every thumbnail is its own HTTP request
authorizing exactly one image, so the whole graph is resolved per tile.

Asserted by which tables are touched rather than by a query count: a count is a
number somebody will update when it moves, while "answering this did not need to
look at friendships" is the property actually being claimed.

`test_photo_visibility_matrix_agreement.py` is the other half of this change -
it pins every answer the gate gives, so this one is free to assert only about
cost.
"""

from __future__ import annotations

from decimal import Decimal

from django.contrib.auth.models import User
from django.db import connection
from django.test.utils import CaptureQueriesContext
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.images.model import Image
from urbanlens.dashboard.models.profile.meta import VisibilityChoice
from urbanlens.dashboard.models.profile.model import Profile

FRIENDSHIPS_TABLE = "dashboard_friendships"
TRIPS_TABLE = "dashboard_trip_memberships"


class _GateCase(TestCase):
    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # the first user is auto-promoted to site admin
        self.viewer = baker.make(User).profile
        self.uploader = baker.make(User).profile
        self.location = baker.make_recipe("dashboard.location", latitude=Decimal("40.0"), longitude=Decimal("-74.0"))
        self.wiki = baker.make("dashboard.Wiki", location=self.location)
        self.image = baker.make(
            Image, profile=self.uploader, wiki=self.wiki, image="pin_images/a.jpg", pending_scan=False
        )
        baker.make_recipe("dashboard.pin", profile=self.viewer, location=self.location)

    def _settings(self, uploader: str, viewer: str) -> None:
        Profile.objects.filter(pk=self.uploader.pk).update(photo_upload_visibility=uploader)
        Profile.objects.filter(pk=self.viewer.pk).update(viewer_photo_filter=viewer)

    def _tables_touched(self) -> str:
        """All SQL run while authorizing the one image, concatenated."""
        viewer = Profile.objects.get(pk=self.viewer.pk)
        with CaptureQueriesContext(connection) as captured:
            Image.objects.filter(pk=self.image.pk).visible_to(viewer).exists()
        return " ".join(query["sql"] for query in captured.captured_queries)


class AnyoneNeedsNoRelationshipsTests(_GateCase):
    def test_an_anyone_photo_does_not_look_at_friendships_or_trips(self) -> None:
        self._settings(VisibilityChoice.ANYONE.value, VisibilityChoice.ANYONE.value)

        sql = self._tables_touched()

        self.assertNotIn(
            FRIENDSHIPS_TABLE, sql, "resolved the viewer's friends to answer a question that cannot depend on them"
        )
        self.assertNotIn(
            TRIPS_TABLE, sql, "resolved the viewer's trips to answer a question that cannot depend on them"
        )

    def test_a_no_one_photo_does_not_look_at_them_either(self) -> None:
        """NO_ONE is decided before any relationship matters, same as ANYONE."""
        self._settings(VisibilityChoice.NO_ONE.value, VisibilityChoice.ANYONE.value)

        sql = self._tables_touched()

        self.assertNotIn(FRIENDSHIPS_TABLE, sql)
        self.assertNotIn(TRIPS_TABLE, sql)


class ButTheOnesThatDoStillLookTests(_GateCase):
    """The anti-vacuity half: a gate that never queried anything would pass the tests above."""

    def test_a_friends_only_photo_does_resolve_friendships(self) -> None:
        self._settings(VisibilityChoice.FRIENDS.value, VisibilityChoice.ANYONE.value)

        self.assertIn(FRIENDSHIPS_TABLE, self._tables_touched())

    def test_a_common_trip_photo_does_resolve_trips(self) -> None:
        self._settings(VisibilityChoice.COMMON_TRIP.value, VisibilityChoice.ANYONE.value)

        self.assertIn(TRIPS_TABLE, self._tables_touched())


class AMatchFoundEarlyStopsLookingTests(_GateCase):
    def test_a_common_pin_match_does_not_go_on_to_resolve_trips(self) -> None:
        """ANYTHING_IN_COMMON tries pin, then friend, then trip - and stops on the first."""
        baker.make_recipe("dashboard.pin", profile=self.uploader, location=self.location)
        self._settings(VisibilityChoice.ANYTHING_IN_COMMON.value, VisibilityChoice.ANYONE.value)

        self.assertNotIn(TRIPS_TABLE, self._tables_touched())


class ARefusedUploaderSkipsTheReachQueryTests(_GateCase):
    def test_nothing_allowed_means_the_wiki_reach_is_never_computed(self) -> None:
        """With no allowed uploader the reach clause matches nothing, so building it is wasted."""
        self._settings(VisibilityChoice.NO_ONE.value, VisibilityChoice.ANYONE.value)

        self.assertNotIn("dashboard_places", self._tables_touched())

    def test_but_an_allowed_uploader_still_computes_it(self) -> None:
        """Anti-vacuity: the reach gate must still run when it can actually admit something."""
        self._settings(VisibilityChoice.ANYONE.value, VisibilityChoice.ANYONE.value)

        self.assertIn("dashboard_places", self._tables_touched())
