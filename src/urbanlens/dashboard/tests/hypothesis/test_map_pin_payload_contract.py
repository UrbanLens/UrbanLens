"""Changing the map pin payload's shape must bump the client cache version."""

from __future__ import annotations

import pathlib
import re

from django.contrib.auth.models import User
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.services.map_pins import MapPinPayloadService

_PIN_CACHE_TS = pathlib.Path(__file__).resolve().parents[2] / "frontend" / "ts" / "shared" / "pin-cache.ts"

#: The payload shape that ``PIN_CACHE_VERSION`` currently describes. Update this
#: *and* the version together, never one alone.
_EXPECTED_VERSION = 11
_EXPECTED_KEYS = frozenset(
    {
        "id",
        "uuid",
        "slug",
        "name",
        "icon",
        "description",
        "priority",
        "last_visited",
        "latitude",
        "longitude",
        "profile",
        "rating",
        "color",
        "label_ids",
        "address",
        "own_icon",
        "own_custom_icon_url",
        "own_color",
        "child_count",
        "cover_photo_url",
    },
)


def _declared_cache_version() -> int:
    """Read ``PIN_CACHE_VERSION`` out of the TypeScript module."""
    match = re.search(r"PIN_CACHE_VERSION\s*=\s*(\d+)", _PIN_CACHE_TS.read_text(encoding="utf-8"))
    assert match is not None, f"PIN_CACHE_VERSION not found in {_PIN_CACHE_TS}"
    return int(match.group(1))


class MapPinPayloadContractTests(TestCase):
    """The payload's key set and the client cache version move together."""

    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # absorbs the bootstrap site-admin promotion
        self.profile = baker.make(User).profile
        location = baker.make(Location, official_name="Payload Contract Place", latitude="40.0", longitude="-74.0")
        self.pin = baker.make(Pin, profile=self.profile, location=location, name="Payload Contract Pin")

    def test_the_snapshot_tracks_the_declared_cache_version(self) -> None:
        self.assertEqual(
            _declared_cache_version(),
            _EXPECTED_VERSION,
            "PIN_CACHE_VERSION changed; update _EXPECTED_KEYS in this test to the payload shape that version describes.",
        )

    def test_the_payload_shape_matches_the_snapshot(self) -> None:
        payload = MapPinPayloadService(self.profile).serialize(self.pin)

        actual = set(payload)
        added = sorted(actual - _EXPECTED_KEYS)
        removed = sorted(_EXPECTED_KEYS - actual)

        self.assertEqual(
            (added, removed),
            ([], []),
            "The map pin payload's shape changed. Bump PIN_CACHE_VERSION in "
            "frontend/ts/shared/pin-cache.ts (and the matching literals in the map "
            "template, which pin-cache.contract.test.ts checks) so cached clients "
            "refetch, then update _EXPECTED_KEYS here. "
            f"added={added} removed={removed}",
        )
