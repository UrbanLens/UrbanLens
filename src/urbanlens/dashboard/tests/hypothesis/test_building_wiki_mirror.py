"""Importing buildings on a *pin* page must not fail on the wiki side.

Reported from staging: adding several buildings from the pin detail page
500'd with `ChildWikiLocationError: There is already a wiki marker at these
exact coordinates`, **after** the child pins had already been created. Three
separate defects behind one traceback:

- A building whose coordinate coincides with an existing wiki marker - very
  commonly the parent wiki itself, since a parcel's coordinate is often a
  building centroid - raised instead of being skipped. One such building
  aborted the whole mirror.
- The mirror ran inline in the request, so a wiki-side failure took down a
  pin-side action that had already succeeded. The user saw a 500 for work
  that was done.
- The mirror did nothing at all when the place had no wiki yet, so the
  community side simply never gained the buildings.

The rule that stands: community pages are *promoted* explicitly, never
created official behind a user's back. A draft is different - drafts are
already auto-created for every pinned location by
`ensure_wiki_for_location`, are invisible until claimed, and are what
this mirrors into.
"""

from __future__ import annotations

from unittest import mock

from django.contrib.auth.models import User
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.place.model import Place, PlaceKind
from urbanlens.dashboard.models.wiki.model import Wiki
from urbanlens.dashboard.models.wiki_edit import WikiEdit
from urbanlens.dashboard.services.pins import pin_restructure

_LAT, _LNG = 41.73332, -73.92794


def _building(seq: int, *, lat: float | None = None, lng: float | None = None) -> dict:
    return {
        "ref": f"cris:{seq}",
        "name": f"Building {seq}",
        "latitude": _LAT + seq / 10000 if lat is None else lat,
        "longitude": _LNG if lng is None else lng,
        "is_on_property": True,
    }


class BuildingWikiMirrorTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # absorbs the bootstrap site-admin promotion
        self.profile = baker.make(User).profile
        self.place = baker.make(Place, kind=PlaceKind.PARCEL)
        self.location = baker.make(Location, latitude=_LAT, longitude=_LNG, place=self.place)
        self.pin = baker.make(Pin, profile=self.profile, location=self.location, parent_pin=None, slug="hrsh")

    def test_a_building_on_the_parent_wikis_own_point_is_skipped_not_fatal(self) -> None:
        """The reported crash: a parcel's coordinate is often a building centroid."""
        wiki = baker.make(Wiki, location=self.location, place=self.place)

        created = pin_restructure.mirror_buildings_to_wiki(
            self.pin,
            [_building(1, lat=_LAT, lng=_LNG), _building(2)],
            self.profile,
        )

        self.assertEqual(created, 1, "the colliding building should be skipped and the rest still mirrored")
        self.assertTrue(wiki.child_wikis.filter(name="Building 2").exists())

    def test_a_place_with_no_wiki_gains_one_rather_than_nothing(self) -> None:
        created = pin_restructure.mirror_buildings_to_wiki(self.pin, [_building(1), _building(2)], self.profile)

        self.assertEqual(created, 2)
        wiki = Wiki.objects.get(location=self.location)
        self.assertEqual(wiki.child_wikis.count(), 2)
        # The gate at the bottom of mirror_buildings_to_wiki ("if created:") is
        # exercised on its positive side here; see the negative side below.
        edit = WikiEdit.objects.get(wiki=wiki)
        self.assertEqual(edit.editor_id, self.profile.pk)
        self.assertEqual(edit.changes, {"child_wikis_imported": {"from": None, "to": "2 building markers"}})

    def test_two_buildings_at_one_point_do_not_abort_each_other(self) -> None:
        wiki = baker.make(Wiki, location=self.location, place=self.place)

        created = pin_restructure.mirror_buildings_to_wiki(
            self.pin,
            [_building(1, lat=_LAT + 0.001), _building(2, lat=_LAT + 0.001), _building(3)],
            self.profile,
        )

        self.assertEqual(created, 2, "the second building at one point is skipped; the third still lands")
        # Count-only would also pass a mutation that dropped Building 3 and kept
        # both colliding buildings by some other accident - pin the actual members.
        names = set(wiki.child_wikis.values_list("name", flat=True))
        self.assertEqual(len(names), 2)
        self.assertIn("Building 3", names)
        self.assertEqual(len(names & {"Building 1", "Building 2"}), 1, "exactly one of the two colliding buildings should survive")

    def test_a_building_already_mirrored_is_not_duplicated(self) -> None:
        """A building the wiki already has a child marker for - e.g. from an
        earlier import - must be matched by ``match_marker`` and skipped, not
        mirrored a second time. None of the tests above exercise this path:
        they all start from a wiki with no children yet."""
        wiki = baker.make(Wiki, location=self.location, place=self.place)
        existing_location = baker.make(Location, latitude=_LAT + 0.0005, longitude=_LNG)
        baker.make(Wiki, parent_wiki=wiki, location=existing_location, name="Building 1")

        created = pin_restructure.mirror_buildings_to_wiki(
            self.pin,
            [_building(1, lat=_LAT + 0.0005, lng=_LNG), _building(2)],
            self.profile,
        )

        self.assertEqual(created, 1, "the already-mirrored building should be matched, not duplicated")
        self.assertEqual(wiki.child_wikis.filter(name="Building 1").count(), 1)
        self.assertTrue(wiki.child_wikis.filter(name="Building 2").exists())

    def test_when_every_building_collides_no_wiki_edit_is_recorded(self) -> None:
        """The WikiEdit audit entry is gated on ``created`` (see the ``if created:``
        guard) - a batch that skips everything must leave the wiki's edit
        history untouched, not log a "0 building markers" entry. The positive
        side of this gate is covered in test_a_place_with_no_wiki_gains_one_rather_than_nothing."""
        wiki = baker.make(Wiki, location=self.location, place=self.place)

        created = pin_restructure.mirror_buildings_to_wiki(self.pin, [_building(1, lat=_LAT, lng=_LNG)], self.profile)

        self.assertEqual(created, 0)
        self.assertFalse(WikiEdit.objects.filter(wiki=wiki).exists())


class BuildingImportRequestTests(TestCase):
    """The pin-side action must survive anything the wiki side does."""

    def setUp(self) -> None:
        super().setUp()
        baker.make(User)
        self.user = baker.make(User)
        self.client.force_login(self.user)
        self.place = baker.make(Place, kind=PlaceKind.PARCEL)
        self.location = baker.make(Location, latitude=_LAT, longitude=_LNG, place=self.place)
        self.pin = baker.make(Pin, profile=self.user.profile, location=self.location, parent_pin=None, slug="hrsh-campus")

    def test_a_wiki_side_failure_does_not_fail_the_import(self) -> None:
        """Pins are already created by then; a 500 reports failure for work that was done.

        The guarantee is structural: the view hands the mirror to
        ``safely_enqueue_task`` rather than calling it in-request, so a real
        worker (a separate process) runs it and nothing it does can reach this
        response. Assert that dispatch directly, rather than relying on the
        mocked RuntimeError actually firing - ``UL_CELERY_TASK_ALWAYS_EAGER``
        is opt-in and False by default (see settings/base.py and the Celery
        note in dashboard/tests/CLAUDE.md). Under a plain container pytest run
        with that unset, ``apply_async`` only enqueues to the in-memory test
        broker and never calls the task body at all, so the exception below
        would silently never fire and this test would pass unconditionally
        even if the view called the wiki mirror synchronously.
        """
        from urbanlens.dashboard.models.cache.location_cache import LocationCache
        from urbanlens.dashboard.services.locations.site_scope import PARCEL_BUILDINGS_CACHE_SOURCE

        LocationCache.set(self.location, PARCEL_BUILDINGS_CACHE_SOURCE, {"buildings": [_building(1), _building(2)], "provider": "redata"}, query_key="k")

        with (
            mock.patch.object(pin_restructure, "mirror_buildings_to_wiki", side_effect=RuntimeError("wiki side is broken")) as mock_mirror,
            mock.patch("urbanlens.dashboard.services.core.celery.safely_enqueue_task") as mock_enqueue,
        ):
            response = self.client.post(f"/dashboard/map/pin/{self.pin.slug}/buildings/import/")

        self.assertEqual(response.status_code, 200, "the wiki mirror must not be able to fail the pin import")
        self.assertEqual(self.pin.detail_pins.count(), 2)
        mock_enqueue.assert_called_once()
        mock_mirror.assert_not_called()
