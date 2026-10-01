"""The snapshot composer writes markup rows with no ceiling at either axis.

The fourth door onto ``PinMarkup``, and the one the H39 audit line actually
named. ``controllers/markup.py`` refuses an oversized ``geometry`` at both of
its own write doors, but the snapshot composer reaches the same table through
``map_snapshot.sanitize_map_data`` -> ``replace_items_from_snapshot``, which
caps neither how many shapes a snapshot carries nor how many points each one
does. Six callers share it: pin comments, wiki comments, visits, memories,
trips and lists - and a wiki comment is read by everyone who opens the page.

The cost is a product the submitter picks alone: ``replace_items_from_snapshot``
saves row by row, and every save fires the two ``PinMarkup`` inference
receivers, so N shapes is N inserts plus 2N receiver runs in one request.

Trimmed rather than refused, unlike the geometry door: ``_sanitize_markup_shapes``
is already a sanitizer that silently drops malformed entries, and the callers
read ``None`` as "no map was submitted" - which makes an existing map get
deleted. Returning fewer shapes is the only direction that neither loses a
map the user still has nor lets one comment set what every later viewer pays.
"""

from __future__ import annotations

import json

from django.conf import settings
from django.test import override_settings
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.markup.model import MarkupMap, PinMarkup
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.services.map.map_snapshot import sanitize_map_data

SHAPES_SETTING = "MARKUP_MAX_SHAPES_PER_SNAPSHOT"
POINTS_SETTING = "MARKUP_MAX_GEOMETRY_POINTS"


def _snapshot(shapes: list[dict]) -> dict:
    return {"center_lat": 40.0, "center_lng": -74.0, "zoom": 13, "markup": shapes}


def _line(points: int) -> dict:
    return {"type": "line", "latlngs": [[40.0 + index * 0.0001, -74.0] for index in range(points)]}


class TheSettingExistsTests(TestCase):
    """`override_settings` will happily invent a name production never reads."""

    def test_the_snapshot_shape_ceiling_is_a_real_setting(self) -> None:
        self.assertTrue(hasattr(settings, SHAPES_SETTING), f"{SHAPES_SETTING} is not a real setting")
        self.assertGreater(getattr(settings, SHAPES_SETTING), 0)


class ShapeCountIsBoundedTests(TestCase):
    """One snapshot cannot ask for an unbounded number of rows."""

    @override_settings(**{SHAPES_SETTING: 5})
    def test_a_snapshot_over_the_ceiling_is_trimmed(self) -> None:
        result = sanitize_map_data(_snapshot([_line(2) for _ in range(40)]))
        assert result is not None  # nosec B101
        self.assertEqual(len(result["markup"]), 5)

    @override_settings(**{SHAPES_SETTING: 5})
    def test_a_snapshot_under_the_ceiling_is_untouched(self) -> None:
        result = sanitize_map_data(_snapshot([_line(2) for _ in range(4)]))
        assert result is not None  # nosec B101
        self.assertEqual(len(result["markup"]), 4)


class PointCountIsBoundedTests(TestCase):
    """One shape cannot carry more points than the other door already refuses."""

    @override_settings(**{POINTS_SETTING: 10})
    def test_a_shape_over_the_point_ceiling_is_trimmed(self) -> None:
        result = sanitize_map_data(_snapshot([_line(500)]))
        assert result is not None  # nosec B101
        self.assertEqual(len(result["markup"][0]["latlngs"]), 10)

    @override_settings(**{POINTS_SETTING: 10})
    def test_a_shape_under_the_point_ceiling_keeps_every_point(self) -> None:
        result = sanitize_map_data(_snapshot([_line(6)]))
        assert result is not None  # nosec B101
        self.assertEqual(len(result["markup"][0]["latlngs"]), 6)


class TheWriteDoorIsBoundedTests(TestCase):
    """The rows actually written, through a real caller.

    The sanitizer is the shared choke point, but nothing proves a caller
    routes through it until a caller does.
    """

    def setUp(self) -> None:
        super().setUp()
        baker.make("auth.User")  # the first user is auto-promoted to site admin
        self.user = baker.make("auth.User")
        self.profile = Profile.objects.get(user=self.user)
        self.client.force_login(self.user)
        self.pin = baker.make(Pin, profile=self.profile, location=baker.make(Location))

    def _post(self, shapes: list[dict]):
        return self.client.post(
            reverse("pin.comments", kwargs={"pin_slug": self.pin.slug}),
            data={"text": "here", "map_data": json.dumps(_snapshot(shapes))},
        )

    @override_settings(**{SHAPES_SETTING: 5})
    def test_a_hostile_snapshot_writes_no_more_rows_than_the_ceiling(self) -> None:
        response = self._post([_line(2) for _ in range(60)])
        self.assertEqual(response.status_code, 200)
        self.assertEqual(PinMarkup.objects.count(), 5)

    @override_settings(**{SHAPES_SETTING: 5})
    def test_an_ordinary_snapshot_still_stores_its_drawing(self) -> None:
        response = self._post([_line(2) for _ in range(3)])
        self.assertEqual(response.status_code, 200)
        self.assertEqual(PinMarkup.objects.count(), 3)
        self.assertEqual(MarkupMap.objects.count(), 1)


class TheReadSideIsBoundedTests(TestCase):
    """`to_snapshot` is the fifth door, and the one other people pay for.

    `MarkupJsonView` is capped, but `MarkupMap.to_snapshot()` is a separate
    read path with no ceiling of its own, and `Comment.map_snapshot` calls it
    per comment - so a wiki thread embeds one unbounded snapshot per comment
    for everyone who opens the page. Rows written before the cap existed are
    still out there, which is the other reason the read side needs its own.
    """

    def setUp(self) -> None:
        super().setUp()
        baker.make("auth.User")  # the first user is auto-promoted to site admin
        self.user = baker.make("auth.User")
        self.profile = Profile.objects.get(user=self.user)
        self.markup_map = baker.make(
            MarkupMap, profile=self.profile, center_latitude=40.0, center_longitude=-74.0, zoom=13
        )

    def _add_items(self, count: int) -> None:
        for index in range(count):
            baker.make(
                PinMarkup,
                parent_map=self.markup_map,
                profile=self.profile,
                markup_type="line",
                geometry={
                    "type": "LineString",
                    "coordinates": [[-74.0, 40.0 + index * 0.001], [-74.001, 40.0 + index * 0.001]],
                },
            )

    @override_settings(MARKUP_MAX_ITEMS_PER_RESPONSE=4)
    def test_a_snapshot_returns_no_more_shapes_than_the_ceiling(self) -> None:
        self._add_items(12)
        self.assertEqual(len(self.markup_map.to_snapshot()["markup"]), 4)

    @override_settings(MARKUP_MAX_ITEMS_PER_RESPONSE=4)
    def test_a_small_map_still_returns_every_shape(self) -> None:
        self._add_items(3)
        self.assertEqual(len(self.markup_map.to_snapshot()["markup"]), 3)

    @override_settings(MARKUP_MAX_ITEMS_PER_RESPONSE=4)
    def test_the_cap_still_reuses_a_prefetched_item_cache(self) -> None:
        """Slicing must not turn one prefetched read back into a query per map."""
        self._add_items(12)
        prefetched = MarkupMap.objects.filter(pk=self.markup_map.pk).prefetch_related("items")
        maps = list(prefetched)
        with self.assertNumQueries(0):
            self.assertEqual(len(maps[0].to_snapshot()["markup"]), 4)


class CloningInheritsTheReadCeilingTests(TestCase):
    """ "Add to my maps" copies what the reader shows, not what the row count says.

    `clone_markup_map` round-trips through `to_snapshot()`, so the read ceiling
    bounds the clone as well. Pinned rather than left implicit: the alternative
    is one button press copying an unbounded number of rows, which is the whole
    class of thing this work removes - and the recipient still gets everything
    the reader would have shown them.
    """

    def setUp(self) -> None:
        super().setUp()
        baker.make("auth.User")  # the first user is auto-promoted to site admin
        self.sender = Profile.objects.get(user=baker.make("auth.User"))
        self.recipient = Profile.objects.get(user=baker.make("auth.User"))
        self.source = baker.make(MarkupMap, profile=self.sender, center_latitude=40.0, center_longitude=-74.0, zoom=13)
        for index in range(9):
            baker.make(
                PinMarkup,
                parent_map=self.source,
                profile=self.sender,
                markup_type="line",
                geometry={
                    "type": "LineString",
                    "coordinates": [[-74.0, 40.0 + index * 0.001], [-74.001, 40.0 + index * 0.001]],
                },
            )

    @override_settings(MARKUP_MAX_ITEMS_PER_RESPONSE=4)
    def test_a_clone_carries_no_more_than_the_reader_shows(self) -> None:
        from urbanlens.dashboard.services.sharing.map_sharing import clone_markup_map

        clone = clone_markup_map(self.source, self.recipient, self.sender)
        self.assertEqual(clone.items.count(), 4)
        self.assertEqual(self.source.items.count(), 9, "the original must not be touched")

    def test_an_ordinary_map_clones_whole(self) -> None:
        from urbanlens.dashboard.services.sharing.map_sharing import clone_markup_map

        clone = clone_markup_map(self.source, self.recipient, self.sender)
        self.assertEqual(clone.items.count(), 9)
