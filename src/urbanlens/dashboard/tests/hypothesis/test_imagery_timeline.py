"""Merging REData's two imagery-timeline shapes into one chronology.

REData answers with dated `captures` and continuous `time_series` ranges
because its sources genuinely differ, and nothing consumed either. A time
slider needs one ordered set of offerings, so they are merged - but the merge
must not flatten away the distinctions that make the two different, because
each one lost produces a specific user-visible failure:

- `continuous: false` means a granule-based source (a satellite overpass) may
  have nothing on a date inside its own range, which REData documents as a
  `404 no_imagery` rather than an error. Losing the flag means offering a date
  and then failing to load it.
- `capture_date_resolved: false` means the date shown is Esri's *publication*
  date, typically months off the real acquisition. Losing it means captioning a
  photograph with a date it was not taken.
- `time_series_asset_uuid` is per layer and never merges across layers, so a
  date is always attributable to the layer it came from.
"""

from __future__ import annotations

from urbanlens.core.tests.testcase import SimpleTestCase
from urbanlens.dashboard.services.locations.imagery_timeline import flatten_timeline, timeline_years


class FlattenTimelineTests(SimpleTestCase):
    def test_captures_and_ranges_both_appear(self) -> None:
        envelope = {
            "captures": [{"captured_on": "2025-03-18", "provider": "esri_wayback", "asset": {"url": "https://x/1.png"}}],
            "providers_timeline": [
                {
                    "provider": "nasa_gibs",
                    "time_series": [{"time_series_asset_uuid": "abc", "continuous": True, "intervals": [{"start": "2000-02-24", "end": "2026-08-06", "step": "P1D"}]}],
                },
            ],
        }

        kinds = [entry["kind"] for entry in flatten_timeline(envelope)]

        self.assertIn("capture", kinds)
        self.assertIn("range", kinds)

    def test_a_granule_source_is_not_promised_as_continuous(self) -> None:
        """Offering a date that 404s is worse than saying the range is patchy."""
        envelope = {
            "providers_timeline": [
                {"provider": "nasa_gibs", "time_series": [{"time_series_asset_uuid": "hls", "continuous": False, "intervals": [{"start": "2013-03-22", "end": "2026-08-06"}]}]},
            ],
        }

        entry = flatten_timeline(envelope)[0]

        self.assertFalse(entry["continuous"])
        self.assertEqual(entry["time_series_asset_uuid"], "hls")

    def test_an_unresolved_esri_date_is_flagged_as_inexact(self) -> None:
        """captured_on is Esri's publication date until REData resolves it."""
        envelope = {"captures": [{"captured_on": "2025-03-18", "provider": "esri_wayback", "capture_date_resolved": False}]}

        self.assertFalse(flatten_timeline(envelope)[0]["date_is_exact"])

    def test_a_resolved_or_absent_flag_reads_as_exact(self) -> None:
        """null means the source publishes no acquisition date; only false is a warning."""
        envelope = {"captures": [{"captured_on": "2025-03-18", "capture_date_resolved": True}, {"captured_on": "2024-01-01"}]}

        self.assertTrue(all(entry["date_is_exact"] for entry in flatten_timeline(envelope)))

    def test_layers_from_one_provider_stay_separate(self) -> None:
        """GIBS publishes four independently addressable layers; merging them loses the address."""
        envelope = {
            "providers_timeline": [
                {
                    "provider": "nasa_gibs",
                    "time_series": [
                        {"time_series_asset_uuid": "modis", "continuous": True, "intervals": [{"start": "2000-02-24", "end": "2026-08-06"}]},
                        {"time_series_asset_uuid": "viirs", "continuous": True, "intervals": [{"start": "2012-01-01", "end": "2026-08-06"}]},
                    ],
                },
            ],
        }

        uuids = {entry["time_series_asset_uuid"] for entry in flatten_timeline(envelope)}

        self.assertEqual(uuids, {"modis", "viirs"})

    def test_newest_first(self) -> None:
        envelope = {"captures": [{"captured_on": "1919-06-01"}, {"captured_on": "2025-03-18"}, {"captured_on": "1870-01-01"}]}

        dates = [entry["captured_on"] for entry in flatten_timeline(envelope)]

        self.assertEqual(dates, ["2025-03-18", "1919-06-01", "1870-01-01"])

    def test_undated_and_malformed_rows_are_dropped_not_crashed_on(self) -> None:
        envelope = {"captures": [{"provider": "x"}, "nonsense", {"captured_on": "2020-01-01"}], "providers_timeline": ["nonsense"]}

        entries = flatten_timeline(envelope)

        self.assertEqual(len(entries), 1)

    def test_an_empty_envelope_is_an_empty_chronology(self) -> None:
        self.assertEqual(flatten_timeline({}), [])


class TimelineYearsTests(SimpleTestCase):
    def test_redatas_own_years_are_preferred(self) -> None:
        """They already account for both shapes; deriving would undercount ranges."""
        self.assertEqual(timeline_years({"years": [2024, 1919, 2024]}), [2024, 1919])

    def test_years_are_derived_when_absent(self) -> None:
        envelope = {"captures": [{"captured_on": "2019-05-01"}, {"captured_on": "2021-08-09"}]}

        self.assertEqual(timeline_years(envelope), [2021, 2019])

    def test_a_malformed_date_does_not_break_derivation(self) -> None:
        envelope = {"captures": [{"captured_on": "soon"}, {"captured_on": "2021-08-09"}]}

        self.assertEqual(timeline_years(envelope), [2021])


class HistoricalCarouselSlideTests(SimpleTestCase):
    """Dated captures reach the satellite carousel; ranges deliberately do not.

    `/imagery/` answers "what can I show for this point now". The timeline
    answers "what dates exist" - and for a site that has been demolished,
    re-roofed or cleared, the older frames are the interesting ones, which is
    the whole reason this application wants them.
    """

    def _provider(self):
        from urbanlens.dashboard.plugins.builtin.satellite_imagery import RedataSatelliteProvider

        return RedataSatelliteProvider()

    def _slides(self, envelope, seen=None):
        from unittest import mock

        provider = self._provider()
        gateway = mock.Mock()
        gateway.get_timeline.return_value = envelope
        with mock.patch.object(
            type(provider),
            "_slide_from_result",
            side_effect=lambda _gw, result, *_args: _FakeSlide(result.get("url", ""), "Esri", "", ""),
        ):
            return list(provider._historical_slides(gateway, 41.7, -73.9, seen if seen is not None else set()))

    def test_a_dated_capture_becomes_a_slide(self) -> None:
        envelope = {"captures": [{"captured_on": "1998-06-01", "provider": "esri_wayback", "asset": {"url": "https://x/old.png"}}]}

        slides = self._slides(envelope)

        self.assertEqual(len(slides), 1)
        self.assertEqual(slides[0].date, "1998-06-01")

    def test_a_continuous_range_does_not_become_a_slide(self) -> None:
        """A range is dates to materialise, not images that already exist."""
        envelope = {
            "providers_timeline": [
                {"provider": "nasa_gibs", "time_series": [{"time_series_asset_uuid": "modis", "continuous": True, "intervals": [{"start": "2000-01-01", "end": "2026-01-01"}]}]},
            ],
        }

        self.assertEqual(self._slides(envelope), [])

    def test_an_unresolved_date_is_labelled_as_published(self) -> None:
        """Captioning Esri's publication date as the acquisition date is a lie of months."""
        envelope = {"captures": [{"captured_on": "2025-03-18", "capture_date_resolved": False, "asset": {"url": "https://x/a.png"}}]}

        self.assertIn("published", self._slides(envelope)[0].date)

    def test_a_capture_already_shown_is_not_duplicated(self) -> None:
        envelope = {"captures": [{"captured_on": "2025-03-18", "asset": {"url": "https://x/current.png"}}]}

        self.assertEqual(self._slides(envelope, seen={"https://x/current.png"}), [])

    def test_an_unavailable_timeline_yields_nothing_rather_than_raising(self) -> None:
        from unittest import mock

        from urbanlens.dashboard.services.apis.locations.redata_context_gateway import LocationContextUnavailableError

        gateway = mock.Mock()
        gateway.get_timeline.side_effect = LocationContextUnavailableError("source_error", "down")

        self.assertEqual(list(self._provider()._historical_slides(gateway, 41.7, -73.9, set())), [])


class _FakeSlide:
    """Stands in for SatelliteSlide, which builds real image sources."""

    def __init__(self, img_src: str, source: str, date: str, detail: str) -> None:
        self.img_src = img_src
        self.source = source
        self.date = date
        self.detail = detail
