"""REData media rows and street-view captures whose source image is gone never reach a gallery, carousel or copy (P325).

REData marks such a row ``attributes.mirror_gone`` (``{"status", "reason", "at"}``, REData
``parcels/services/mirror_state.py``): its ``thumbnail_url`` does not load, REData never mirrors it, and its download
answers 404. Both of REData's ways in - the cache-only ``/locations/context/`` read and each domain's own endpoint - pass
through ``services.locations.redata_point_data``, so the mark is acted on there, before the shared answer is cached.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import TYPE_CHECKING, Any
from unittest import mock

from django.contrib.auth.models import User
from django.core.cache import cache
from model_bakery import baker

from hypothesis import given, settings as hyp_settings, strategies as st
from urbanlens.core.tests.testcase import SimpleTestCase, TestCase
from urbanlens.dashboard.plugins.builtin.redata_aerial_media import AerialMediaSource
from urbanlens.dashboard.plugins.builtin.redata_nearby_media import NearbyMediaSource
from urbanlens.dashboard.plugins.builtin.redata_street_level import StreetLevelPhotosSource
from urbanlens.dashboard.services.apis.locations.redata_context_gateway import (
    LocationContextEnvelope,
    LocationContextUnavailableError,
)
from urbanlens.dashboard.services.apis.locations.redata_locations_context_gateway import (
    LocationsContext,
    RedataLocationsContextGateway,
)
from urbanlens.dashboard.services.apis.locations.redata_media_gateway import (
    NEARBY_MEDIA_PROVIDERS,
    MapillaryStreetViewProvider,
    RedataMediaGateway,
)
from urbanlens.dashboard.services.apis.locations.redata_street_view_gateway import RedataStreetViewGateway
from urbanlens.dashboard.services.locations import redata_point_data
from urbanlens.dashboard.services.locations.redata_point_data import REASON_UNKNOWN_PROVIDER, mirror_gone
from urbanlens.UrbanLens.settings.app import settings

if TYPE_CHECKING:
    from collections.abc import Iterator

_LATITUDE = 41.7
_LONGITUDE = -73.9
#: REData's mark, as ``mirror_state.after_gone`` writes it.
_GONE = {"status": 404, "reason": "missing", "at": "2026-10-05T03:00:00+00:00"}
_ARCHIVED = {"status": 409, "reason": "archived", "at": "2026-10-05T03:00:00+00:00"}


@contextmanager
def _redata_on() -> Iterator[None]:
    with (
        mock.patch.object(settings, "redata_api_url", "https://redata.example.test"),
        mock.patch.object(settings, "redata_api_key", "test-key"),
    ):
        yield


def _media_row(uuid: str, **overrides: Any) -> dict[str, Any]:
    """A row in REData's ``MediaItemSerializer`` shape."""
    row: dict[str, Any] = {
        "uuid": uuid,
        "provider": "wikimedia_commons",
        "external_id": f"File:{uuid}.jpg",
        "kind": "photo",
        "title": f"Photo {uuid}",
        "description": "",
        "url": f"https://commons.wikimedia.org/wiki/File:{uuid}.jpg",
        "thumbnail_url": f"https://upload.wikimedia.org/thumb/{uuid}.jpg",
        "cached_url": "",
        "credit": "Jane Doe",
        "latitude": _LATITUDE,
        "longitude": _LONGITUDE,
        "attributes": {"license": "CC BY-SA 4.0"},
        "is_aerial": False,
    }
    row.update(overrides)
    return row


def _capture(uuid: str, provider: str, captured_on: str, latitude: float, **overrides: Any) -> dict[str, Any]:
    """A row in REData's ``StreetViewCaptureSerializer`` shape."""
    row: dict[str, Any] = {
        "uuid": uuid,
        "provider": provider,
        "external_id": f"{provider}-{uuid}",
        "sequence_id": "seq-1",
        "latitude": latitude,
        "longitude": _LONGITUDE,
        "captured_at": f"{captured_on}T12:00:00Z",
        "captured_on": captured_on,
        "heading_degrees": 90.0,
        "is_panoramic": False,
        "image_url": f"https://images.example.test/{provider}/{uuid}.jpg",
        "thumbnail_url": f"https://images.example.test/{provider}/{uuid}-thumb.jpg",
        "download_url": f"https://redata.example.test/api/v1/street-view/{uuid}/download/",
        "cached": False,
        "credit": "volunteer",
        "license": "CC BY-SA",
        "attributes": {},
    }
    row.update(overrides)
    return row


def _gone(row: dict[str, Any], mark: object = _GONE) -> dict[str, Any]:
    return {**row, "attributes": {**row.get("attributes", {}), "mirror_gone": mark}}


def _envelope(results: list[dict[str, Any]], *, complete: bool = True) -> LocationContextEnvelope:
    return LocationContextEnvelope(count=len(results), complete=complete, results=results, providers=[])


def _timeline(*dates: dict[str, Any], complete: bool = True) -> dict[str, Any]:
    return {"dates": list(dates), "complete": complete, "providers": []}


def _date(
    representative: dict[str, Any], *, count: int = 1, captures: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "captured_on": representative["captured_on"],
        "provider": representative["provider"],
        "count": count,
        "is_panoramic": bool(representative.get("is_panoramic")),
        "representative": representative,
    }
    if captures is not None:
        entry["captures"] = captures
    return entry


_UUIDS = [f"{index:08d}-1111-4111-8111-111111111111" for index in range(8)]

# JSON values as REData's body could carry them.
_JSON = st.recursive(
    st.none() | st.booleans() | st.integers() | st.floats(allow_nan=False) | st.text(max_size=8),
    lambda children: st.lists(children, max_size=4) | st.dictionaries(st.text(max_size=8), children, max_size=4),
    max_leaves=12,
)
#: A pure function, so a slow example is a busy host, not a slow answer.
_hyp = hyp_settings(deadline=None)


class MirrorGoneTests(SimpleTestCase):
    """``mirror_gone`` reads REData's mark in the form REData sends, and nothing else."""

    def test_redatas_mark_is_gone_whatever_its_reason(self) -> None:
        for mark in (_GONE, _ARCHIVED, True):
            with self.subTest(mark=mark):
                self.assertTrue(mirror_gone(_gone(_media_row(_UUIDS[0]), mark)))

    def test_a_row_without_a_truthy_mark_is_not_gone(self) -> None:
        for mark in (None, False, {}, "", 0, []):
            with self.subTest(mark=mark):
                self.assertFalse(mirror_gone(_gone(_media_row(_UUIDS[0]), mark)))
        self.assertFalse(mirror_gone(_media_row(_UUIDS[0])))

    def test_malformed_rows_are_not_gone_and_never_raise(self) -> None:
        malformed: list[object] = [
            None,
            "mirror_gone",
            ["attributes"],
            42,
            {},
            {"attributes": None},
            {"attributes": "mirror_gone"},
            {"attributes": ["mirror_gone"]},
            {"attributes": 1},
            {"mirror_gone": _GONE},
        ]
        for row in malformed:
            with self.subTest(row=row):
                self.assertFalse(mirror_gone(row))

    @_hyp
    @given(row=_JSON)
    def test_any_json_value_answers_a_bool(self, row: object) -> None:
        answer = mirror_gone(row)

        self.assertIsInstance(answer, bool)
        if answer:
            assert isinstance(row, dict)
            self.assertIsInstance(row.get("attributes"), dict)

    @_hyp
    @given(
        mark=_JSON,
        other=st.dictionaries(st.text(max_size=8).filter(lambda key: key != "mirror_gone"), _JSON, max_size=3),
    )
    def test_gone_exactly_when_the_mark_is_truthy(self, mark: object, other: dict[str, object]) -> None:
        row = {"uuid": _UUIDS[0], "attributes": {**other, "mirror_gone": mark}}

        self.assertEqual(mirror_gone(row), bool(mark))

    @_hyp
    @given(attributes=_JSON.filter(lambda value: not isinstance(value, dict)))
    def test_attributes_that_are_not_a_mapping_are_never_gone(self, attributes: object) -> None:
        self.assertFalse(mirror_gone({"uuid": _UUIDS[0], "attributes": attributes}))


class GoneMediaLeftOutTests(SimpleTestCase):
    """``media_near`` drops gone rows on both of REData's paths, before the shared answer is cached."""

    def setUp(self) -> None:
        super().setUp()
        self.live = _media_row(_UUIDS[0])
        self.gone = _gone(_media_row(_UUIDS[1]))
        self.archived = _gone(_media_row(_UUIDS[2], provider="flickr"), _ARCHIVED)
        self.malformed = [
            _media_row(_UUIDS[3], attributes=None),
            _media_row(_UUIDS[4], attributes=["mirror_gone"]),
            _media_row(_UUIDS[5], attributes={"mirror_gone": None}),
            _media_row(_UUIDS[6], attributes={"mirror_gone": False}),
        ]
        self.rows = [self.live, self.gone, self.archived, *self.malformed]

    def test_the_context_read_leaves_gone_rows_out(self) -> None:
        context = LocationsContext(domains={"media": _envelope(self.rows)})
        with (
            _redata_on(),
            mock.patch.object(RedataLocationsContextGateway, "get_context", return_value=context),
            mock.patch.object(RedataMediaGateway, "lookup_envelope") as lookup,
        ):
            envelope = redata_point_data.media_near(_LATITUDE, _LONGITUDE)
        lookup.assert_not_called()

        self.assertEqual(
            [row["uuid"] for row in envelope.results], [_UUIDS[0], _UUIDS[3], _UUIDS[4], _UUIDS[5], _UUIDS[6]]
        )
        self.assertEqual(envelope.count, 5)
        self.assertTrue(envelope.complete)

    def test_the_lookup_leaves_gone_rows_out(self) -> None:
        with (
            _redata_on(),
            mock.patch.object(RedataLocationsContextGateway, "get_context", return_value=LocationsContext()),
            mock.patch.object(RedataMediaGateway, "lookup_envelope", return_value=_envelope(self.rows, complete=False)),
        ):
            envelope = redata_point_data.media_near(_LATITUDE, _LONGITUDE)

        self.assertEqual(
            [row["uuid"] for row in envelope.results], [_UUIDS[0], _UUIDS[3], _UUIDS[4], _UUIDS[5], _UUIDS[6]]
        )
        self.assertEqual(envelope.count, 5)
        self.assertFalse(envelope.complete, "Leaving rows out must not turn a partial answer into a whole one.")

    def test_the_shared_answer_is_cached_already_filtered(self) -> None:
        with (
            _redata_on(),
            mock.patch.object(RedataLocationsContextGateway, "get_context", return_value=LocationsContext()),
            mock.patch.object(RedataMediaGateway, "lookup_envelope", return_value=_envelope(self.rows)),
        ):
            redata_point_data.media_near(_LATITUDE, _LONGITUDE)
        shared = cache.get(redata_point_data.media_key(_LATITUDE, _LONGITUDE, NEARBY_MEDIA_PROVIDERS))

        assert isinstance(shared, tuple)
        self.assertNotIn(_UUIDS[1], [row["uuid"] for row in shared[0].results])
        self.assertNotIn(_UUIDS[2], [row["uuid"] for row in shared[0].results])

    def test_the_unfiltered_retry_after_an_unknown_provider_leaves_gone_rows_out_under_both_keys(self) -> None:
        """#242's retry with no provider filter is shared under its own key as well; neither copy may hold a gone row."""
        refused = LocationContextUnavailableError(REASON_UNKNOWN_PROVIDER, "Unknown provider(s): vimeo.", rejected=True)
        with (
            _redata_on(),
            mock.patch.object(RedataLocationsContextGateway, "get_context", return_value=LocationsContext()),
            mock.patch.object(
                RedataMediaGateway, "lookup_envelope", side_effect=[refused, _envelope(self.rows)]
            ) as lookup,
            self.assertLogs(redata_point_data.logger, level="WARNING"),
        ):
            envelope = redata_point_data.media_near(_LATITUDE, _LONGITUDE)
        self.assertEqual(lookup.call_count, 2)

        kept = [_UUIDS[0], _UUIDS[3], _UUIDS[4], _UUIDS[5], _UUIDS[6]]
        self.assertEqual([row["uuid"] for row in envelope.results], kept)
        self.assertEqual(envelope.count, 5)
        for providers in (None, NEARBY_MEDIA_PROVIDERS):
            shared = cache.get(redata_point_data.media_key(_LATITUDE, _LONGITUDE, providers))
            assert isinstance(shared, tuple), providers
            self.assertEqual([row["uuid"] for row in shared[0].results], kept, providers)

    def test_the_context_answer_other_domains_share_is_left_as_it_was(self) -> None:
        media = _envelope(self.rows)
        with (
            _redata_on(),
            mock.patch.object(
                RedataLocationsContextGateway, "get_context", return_value=LocationsContext(domains={"media": media})
            ),
        ):
            redata_point_data.media_near(_LATITUDE, _LONGITUDE)

        self.assertEqual(len(media.results), len(self.rows))
        self.assertEqual(media.count, len(self.rows))

    def test_an_answer_with_nothing_gone_is_returned_as_it_came(self) -> None:
        envelope = _envelope([self.live, *self.malformed])
        with (
            _redata_on(),
            mock.patch.object(
                RedataLocationsContextGateway, "get_context", return_value=LocationsContext(domains={"media": envelope})
            ),
        ):
            self.assertEqual(redata_point_data.media_near(_LATITUDE, _LONGITUDE), envelope)


class GoneCapturesLeftOutTests(SimpleTestCase):
    """``street_view_dates`` never offers a gone frame as a date's picture, on either of REData's paths."""

    def _from_context(self, captures: list[dict[str, Any]]) -> redata_point_data.StreetViewDates:
        context = LocationsContext(domains={"street_view": _envelope(captures)})
        with (
            _redata_on(),
            mock.patch.object(RedataLocationsContextGateway, "get_context", return_value=context),
            mock.patch.object(RedataStreetViewGateway, "get_timeline") as timeline,
        ):
            found = redata_point_data.street_view_dates(_LATITUDE, _LONGITUDE)
        timeline.assert_not_called()
        return found

    def _from_timeline(self, timeline: dict[str, Any]) -> redata_point_data.StreetViewDates:
        with (
            _redata_on(),
            mock.patch.object(RedataLocationsContextGateway, "get_context", return_value=LocationsContext()),
            mock.patch.object(RedataStreetViewGateway, "get_timeline", return_value=timeline),
        ):
            return redata_point_data.street_view_dates(_LATITUDE, _LONGITUDE)

    def test_the_context_read_picks_the_nearest_frame_still_there(self) -> None:
        nearest = _gone(_capture(_UUIDS[0], "kartaview", "2017-05-03", 41.70001), _ARCHIVED)
        farther = _capture(_UUIDS[1], "kartaview", "2017-05-03", 41.7004)
        found = self._from_context([nearest, farther])

        self.assertEqual(len(found.dates), 1)
        self.assertEqual(found.dates[0]["representative"]["uuid"], _UUIDS[1])
        self.assertEqual(found.dates[0]["count"], 1)

    def test_the_context_read_drops_a_date_whose_every_frame_is_gone(self) -> None:
        captures = [
            _gone(_capture(_UUIDS[0], "kartaview", "2016-04-01", 41.70001)),
            _gone(_capture(_UUIDS[1], "kartaview", "2016-04-01", 41.7002), True),
            _capture(_UUIDS[2], "mapillary", "2022-08-09", 41.7001, attributes=None),
        ]
        found = self._from_context(captures)

        self.assertEqual(
            [(entry["provider"], entry["captured_on"]) for entry in found.dates], [("mapillary", "2022-08-09")]
        )

    def test_the_timeline_drops_a_date_whose_representative_is_gone_when_it_names_no_other_frame(self) -> None:
        kept = _date(_capture(_UUIDS[0], "mapillary", "2021-06-01", 41.7))
        dropped = _date(_gone(_capture(_UUIDS[1], "kartaview", "2017-05-03", 41.7)), count=5)
        found = self._from_timeline(_timeline(dropped, kept, complete=False))

        self.assertEqual([entry["representative"]["uuid"] for entry in found.dates], [_UUIDS[0]])
        self.assertFalse(found.complete)

    def test_the_timeline_stands_in_the_nearest_frame_still_there_when_it_names_the_others(self) -> None:
        representative = _gone(_capture(_UUIDS[0], "kartaview", "2017-05-03", 41.70001))
        farthest = _capture(_UUIDS[1], "kartaview", "2017-05-03", 41.7009)
        nearer = _capture(_UUIDS[2], "kartaview", "2017-05-03", 41.7003, is_panoramic=True)
        also_gone = _gone(_capture(_UUIDS[3], "kartaview", "2017-05-03", 41.70002), _ARCHIVED)
        entry = _date(representative, count=4, captures=[representative, farthest, nearer, also_gone])
        found = self._from_timeline(_timeline(entry))

        self.assertEqual(len(found.dates), 1)
        date = found.dates[0]
        self.assertEqual(date["representative"]["uuid"], _UUIDS[2])
        self.assertEqual(date["count"], 2)
        self.assertTrue(date["is_panoramic"])
        self.assertEqual([capture["uuid"] for capture in date["captures"]], [_UUIDS[1], _UUIDS[2]])

    def test_the_timeline_drops_a_date_whose_named_frames_are_all_gone(self) -> None:
        representative = _gone(_capture(_UUIDS[0], "kartaview", "2017-05-03", 41.7))
        other = _gone(_capture(_UUIDS[1], "kartaview", "2017-05-03", 41.7001), True)
        found = self._from_timeline(_timeline(_date(representative, count=2, captures=[representative, other])))

        self.assertEqual(found.dates, [])

    def test_the_timeline_keeps_dates_whose_representative_is_there_or_malformed(self) -> None:
        dates = [
            _date(_capture(_UUIDS[0], "mapillary", "2021-06-01", 41.7), count=3),
            _date(_capture(_UUIDS[1], "panoramax", "2020-01-01", 41.7, attributes="mirror_gone")),
            {"captured_on": "2019-01-01", "provider": "kartaview", "count": 1, "representative": None},
        ]
        found = self._from_timeline(_timeline(*dates))

        self.assertEqual([entry["captured_on"] for entry in found.dates], ["2021-06-01", "2020-01-01", "2019-01-01"])
        self.assertEqual(found.dates[0], dates[0])


def _pin():
    location = baker.make("dashboard.Location", latitude=_LATITUDE, longitude=_LONGITUDE)
    return baker.make_recipe("dashboard.pin", profile=baker.make(User).profile, location=location)


class GalleriesShowNoGoneItemTests(TestCase):
    """The Nearby Media, Aerial and Street-level tabs cache and show no tile for a gone item."""

    def setUp(self) -> None:
        super().setUp()
        self.pin = _pin()

    def test_nearby_and_aerial_tabs_leave_gone_rows_out(self) -> None:
        from urbanlens.dashboard.models.cache.location_cache import LocationCache

        rows = [
            _media_row(_UUIDS[0], title="Mill from the river"),
            _gone(_media_row(_UUIDS[1], title="Gone from Commons")),
            _media_row(_UUIDS[2], provider="youtube", is_aerial=True, title="Drone over the mill"),
            _gone(_media_row(_UUIDS[3], provider="youtube", is_aerial=True, title="Gone drone"), _ARCHIVED),
        ]
        with (
            _redata_on(),
            mock.patch.object(RedataLocationsContextGateway, "get_context", return_value=LocationsContext()),
            mock.patch.object(RedataMediaGateway, "lookup_envelope", return_value=_envelope(rows)),
        ):
            NearbyMediaSource().fetch(self.pin)
            AerialMediaSource().fetch(self.pin)

        nearby = LocationCache.get_fresh(self.pin.location, "redata_media")
        aerial = LocationCache.get_fresh(self.pin.location, "redata_aerial")
        assert nearby is not None and aerial is not None
        self.assertEqual(
            [item.caption for item in NearbyMediaSource().media_items(nearby.data)], ["Mill from the river"]
        )
        self.assertEqual(
            [item.caption for item in AerialMediaSource().media_items(aerial.data)], ["Drone over the mill"]
        )

    def test_the_street_level_tab_shows_no_gone_frame(self) -> None:
        from urbanlens.dashboard.models.cache.location_cache import LocationCache

        live = _capture(_UUIDS[0], "mapillary", "2021-06-01", 41.7)
        gone = _gone(_capture(_UUIDS[1], "kartaview", "2017-05-03", 41.7), _ARCHIVED)
        with (
            _redata_on(),
            mock.patch.object(RedataLocationsContextGateway, "get_context", return_value=LocationsContext()),
            mock.patch.object(
                RedataStreetViewGateway, "get_timeline", return_value=_timeline(_date(gone), _date(live))
            ),
        ):
            StreetLevelPhotosSource().fetch(self.pin)

        cached = LocationCache.get_fresh(self.pin.location, "redata_street_level")
        assert cached is not None
        thumbs = [item.thumb_url for item in StreetLevelPhotosSource().media_items(cached.data)]
        self.assertEqual(thumbs, [live["thumbnail_url"]])


class CarouselShowsNoGoneFrameTests(SimpleTestCase):
    """The street-view carousel's slides never show a gone frame."""

    def test_the_timeline_path(self) -> None:
        live = _capture(_UUIDS[0], "mapillary", "2021-06-01", 41.7)
        gone = _gone(_capture(_UUIDS[1], "mapillary", "2018-03-02", 41.7))
        with (
            _redata_on(),
            mock.patch.object(RedataLocationsContextGateway, "get_context", return_value=LocationsContext()),
            mock.patch.object(
                RedataStreetViewGateway, "get_timeline", return_value=_timeline(_date(gone), _date(live))
            ),
        ):
            slides = list(MapillaryStreetViewProvider()._generate_street_view_slides(_LATITUDE, _LONGITUDE))

        self.assertEqual([slide.img_src for slide in slides], [live["image_url"]])

    def test_the_context_path(self) -> None:
        gone = _gone(_capture(_UUIDS[0], "mapillary", "2018-03-02", 41.70001))
        stand_in = _capture(_UUIDS[1], "mapillary", "2018-03-02", 41.7005)
        context = LocationsContext(domains={"street_view": _envelope([gone, stand_in])})
        with (
            _redata_on(),
            mock.patch.object(RedataLocationsContextGateway, "get_context", return_value=context),
        ):
            slides = list(MapillaryStreetViewProvider()._generate_street_view_slides(_LATITUDE, _LONGITUDE))

        self.assertEqual([slide.img_src for slide in slides], [stand_in["image_url"]])
