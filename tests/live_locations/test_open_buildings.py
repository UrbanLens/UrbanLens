"""Google Open Buildings answers for a real building where it has coverage, read from Google's own bucket.

Off unless ``UL_LIVE_LOCATIONS=1``; needs no REData. Downloads one 37 MiB level-6 shard per run, so it runs through
``bin/host_pytest.sh`` like the pipeline test:

    UL_LIVE_LOCATIONS=1 UL_TEST_DB_NAME=test_<unique> bin/host_pytest.sh --reuse-db tests/live_locations/test_open_buildings.py

The Royal Palace (Haw Kham) in Luang Prabang, Laos, at its Wikipedia coordinate. Its level-4 shard is 3.5 GB
compressed, past the cap, so before the gateway read level-6 shards (P319) this answered nothing.
"""

from __future__ import annotations

from django.contrib.gis.geos import Point
import pytest

pytestmark = [pytest.mark.live_source, pytest.mark.django_db]

_PALACE = (19.8921, 102.1356)
#: Its footprint in v3 is 2,382 m², about 65 by 69 m; bounds, not the value, so a re-detected outline still passes.
_PALACE_AREA_SQM = (1500.0, 3500.0)


def test_the_royal_palace_in_luang_prabang_has_its_footprint() -> None:
    from urbanlens.dashboard.services.apis.locations.boundaries.google_open_buildings import GoogleOpenBuildingsGateway
    from urbanlens.dashboard.services.geo.area import area_sqm

    latitude, longitude = _PALACE
    boundary = GoogleOpenBuildingsGateway().get_boundary(latitude, longitude)

    assert boundary is not None, "Open Buildings returned no footprint for the Royal Palace"
    assert boundary.contains(Point(longitude, latitude, srid=4326))
    low, high = _PALACE_AREA_SQM
    assert low <= area_sqm(boundary) <= high, f"the footprint is {area_sqm(boundary):.0f} m²"
