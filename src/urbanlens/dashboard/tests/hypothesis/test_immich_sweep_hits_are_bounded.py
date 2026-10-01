"""The library sweep built one hit per photo, for a fix that already existed.

H01's last piece. `sweep_immich_library_locations` appends a `LocationHit` for
every geotagged asset in the library and holds them all until the sweep ends, so
peak memory tracks the size of someone's photo library rather than the number of
places in it. The worker it runs on has no `mem_limit`, so that is host memory.

`LocationHit.weight` exists precisely for this, and its own docstring records the
same bug being fixed on the *other* ingest path: the local-scan upload used to
expand a cluster's `count` into that many identical synthetic hits, and "a scan
with a few hundred clusters averaging hundreds of photos each could balloon into
hundreds of thousands of hits". `controllers.tools._parse_cluster` now emits one
weighted hit per cluster. The Immich sweep never got the same treatment.

So the bound here is not a new ceiling, it is the existing mechanism applied to
the path that was missed: collapse assets sharing a place into one weighted hit,
keeping up to `MAX_SUGGESTION_PHOTOS` sample assets so the review queue still has
thumbnails. Photos are not dropped - `weight` carries the count and `extra_dates`
carries the dates, which is what every downstream consumer actually reads.

Bucketed at four decimal places, about 11m, chosen to sit well inside
`CLUSTER_RADIUS_M` (50m): collapsing points that clustering would merge anyway
cannot change which cluster they land in.
"""

from __future__ import annotations

import datetime
from unittest import mock

from django.contrib.auth.models import User
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.immich.model import ImmichAccount
from urbanlens.dashboard.models.pin_suggestions.model import MAX_SUGGESTION_PHOTOS
from urbanlens.dashboard.services.apis.immich.gateway import SearchAsset
from urbanlens.dashboard.services.pins.pin_suggestions import IngestSummary
from urbanlens.dashboard.tasks import sweep_immich_library_locations

GATEWAY = "urbanlens.dashboard.services.apis.immich.ImmichGateway"
INGEST = "urbanlens.dashboard.services.pins.pin_suggestions.ingest_location_hits"


def _asset(index: int, lat: float, lon: float) -> SearchAsset:
    return SearchAsset(
        id=f"asset-{index}",
        taken_at=datetime.datetime(2024, 1, 1, 12, 0, tzinfo=datetime.UTC) + datetime.timedelta(days=index % 7),
        lat=lat,
        lon=lon,
        city="Somewhere",
    )


class _SweepCase(TestCase):
    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # the first user is auto-promoted to site admin
        self.profile = baker.make(User).profile
        ImmichAccount.objects.create(profile=self.profile, server_url="https://photos.example.com", api_key="k")

    def _run_sweep(self, assets: list[SearchAsset]) -> list:
        """Run the sweep over *assets*, returning the hits it handed to ingest."""
        captured: list = []

        def _capture(profile, hits, origin):
            captured.extend(hits)
            return IngestSummary(matched_suggestions=0, new_pin_suggestions=0, hits_processed=0)

        gateway = mock.MagicMock()
        gateway.library_asset_count.return_value = len(assets)
        gateway.iter_library_assets.return_value = iter([(assets, len(assets))])

        with mock.patch(GATEWAY, return_value=gateway), mock.patch(INGEST, side_effect=_capture):
            sweep_immich_library_locations.apply(args=(self.profile.pk,))
        return captured


class OneHitPerPhotoTests(_SweepCase):
    def test_a_thousand_photos_in_one_place_do_not_become_a_thousand_hits(self) -> None:
        assets = [_asset(index, 40.0 + (index % 3) * 0.000001, -74.0) for index in range(1000)]

        hits = self._run_sweep(assets)

        self.assertLessEqual(
            len(hits),
            MAX_SUGGESTION_PHOTOS,
            f"1000 photos at one place produced {len(hits)} hits; the sweep still holds one per photo",
        )

    def test_the_hit_count_tracks_places_not_photos(self) -> None:
        few = self._run_sweep([_asset(i, 40.0, -74.0) for i in range(50)])
        many = self._run_sweep([_asset(i, 40.0, -74.0) for i in range(5000)])

        self.assertEqual(len(many), len(few), "the same one place cost more hits when it held more photos")


class NoPhotoIsLostTests(_SweepCase):
    """The anti-vacuity half: returning no hits at all would pass the tests above."""

    def test_the_weights_still_sum_to_every_geotagged_photo(self) -> None:
        assets = [_asset(index, 40.0, -74.0) for index in range(1000)]

        hits = self._run_sweep(assets)

        self.assertEqual(sum(hit.weight for hit in hits), 1000)

    def test_distinct_places_still_produce_distinct_hits(self) -> None:
        assets = [_asset(index, 40.0 + index * 0.01, -74.0) for index in range(12)]

        hits = self._run_sweep(assets)

        self.assertEqual(len({(round(hit.latitude, 3), round(hit.longitude, 3)) for hit in hits}), 12)

    def test_every_capture_date_survives_the_collapse(self) -> None:
        """`extra_dates` is what carries a cluster's date spread into visit_dates."""
        assets = [_asset(index, 40.0, -74.0) for index in range(100)]
        expected = {asset.taken_at.date().isoformat() for asset in assets}

        hits = self._run_sweep(assets)

        seen = {hit.taken_at.date().isoformat() for hit in hits}
        for hit in hits:
            seen.update(hit.extra_dates)
        self.assertEqual(seen, expected)

    def test_sample_assets_are_kept_for_the_review_queue(self) -> None:
        assets = [_asset(index, 40.0, -74.0) for index in range(100)]

        hits = self._run_sweep(assets)

        self.assertEqual(len({hit.asset_id for hit in hits if hit.asset_id}), MAX_SUGGESTION_PHOTOS)


class UngeotaggedPhotosAreStillSkippedTests(_SweepCase):
    def test_an_asset_with_no_gps_contributes_nothing(self) -> None:
        assets = [SearchAsset(id="no-gps", taken_at=datetime.datetime(2024, 1, 1, tzinfo=datetime.UTC))]

        self.assertEqual(self._run_sweep(assets), [])
