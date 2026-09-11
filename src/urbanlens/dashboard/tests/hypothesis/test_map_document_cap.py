"""The map's whole-account endpoints must have a ceiling the account cannot raise.

`MapController.map_data_context` (`controllers/maps.py:1149-1178`) serialises every
root pin the profile owns - `MapPinPayloadService.all()`, whose own docstring says
"Unbounded in output by design" - and hands the list to `map/data.html`, which
embeds it as one JSON document. Both `search_map_post` (`:520-538`) and `init_map`
(`:1102-1103`) go through it, neither passes a limit, and neither is cached.

R27 made that cheap per pin: 10,000 pins now cost ~0.33s of CPU instead of 5.8s.
Cheap per pin is not the same as bounded. The response is still O(account), so the
cost of one request is still set by how many pins the requester happens to own,
and the *keyset-paginated* sibling endpoint (`map.pins`, clamped to
`MAX_LIMIT = 1000`) exists precisely because that is not an acceptable shape for a
request path.

**These tests fail today, deliberately** - the TDD reproduction, under
`xfail(strict=True)` so they turn red the day a ceiling lands and prompt the
markers' removal.

The first test asserts the setting *exists* before anything overrides it, and that
ordering is load-bearing rather than pedantic: `override_settings` will happily
invent a name production does not have, so a test that goes straight to
overriding `UL_MAP_DOCUMENT_MAX_PINS` would configure a ceiling nothing reads and
then pass against code that has none.
"""

from __future__ import annotations

import json
import re
from typing import Any

from django.conf import settings
from django.contrib.auth.models import User
from django.test import override_settings
from django.urls import reverse
from model_bakery import baker
import pytest

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.profile.model import Profile

#: The setting the ceiling should live behind. Named here rather than imported so
#: this module states the requirement even while the name does not exist.
SETTING_NAME = "UL_MAP_DOCUMENT_MAX_PINS"

#: Stand-in ceiling, so the test states the rule without seeding 30,001 pins.
TEST_CAP = 4

#: Comfortably past the stand-in ceiling.
SEEDED = 10

_JSON_SCRIPT = re.compile(
    r'<script[^>]*id="map-filter-pins"[^>]*>(.*?)</script>',
    re.DOTALL,
)


def _embedded_pins(html: str) -> list[dict[str, Any]]:
    """The pin document `map/data.html` embedded, parsed.

    Args:
        html: The rendered response body.

    Returns:
        One dict per pin the page shipped to the client.

    Raises:
        AssertionError: The document is missing, which would make every
            count assertion below pass without measuring anything.
    """
    match = _JSON_SCRIPT.search(html)
    if match is None:
        raise AssertionError(
            "no #map-filter-pins document in the response - the page shape changed, and a "
            "pin count taken from it would be vacuous",
        )
    return json.loads(match.group(1))


class _SeededMapCase(TestCase):
    """A profile with more pins than any reasonable single document should carry."""

    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # absorbs the bootstrap site-admin promotion
        self.user = baker.make(User)
        self.profile: Profile = self.user.profile
        self.client.force_login(self.user)
        self._seeded = 0
        self.seed_pins(SEEDED)

    def seed_pins(self, count: int) -> None:
        """Create *count* root pins for this profile."""
        for _ in range(count):
            self._seeded += 1
            # Locations are unique on (latitude, longitude).
            location = baker.make(
                Location,
                latitude=f"38.{self._seeded:06d}",
                longitude=f"-77.{self._seeded:06d}",
                official_name="Ceiling Place",
            )
            baker.make(Pin, profile=self.profile, location=location)


class TheCeilingMustExistTests(_SeededMapCase):
    """Before anything can be bounded, something has to name the bound."""

    @pytest.mark.xfail(strict=True, reason="P96-adjacent: the map document has no ceiling yet; D12 adds one")
    def test_a_setting_names_the_maximum_pins_in_one_document(self) -> None:
        self.assertTrue(
            hasattr(settings, SETTING_NAME),
            f"{SETTING_NAME} does not exist, so nothing bounds a whole-account map document - "
            "and an override_settings of that name would silently configure a ceiling no code reads.",
        )


@override_settings(**{SETTING_NAME: TEST_CAP})
class TheFilterPostMustRespectItTests(_SeededMapCase):
    """`map.search` is the one a user triggers repeatedly, by changing a filter."""

    @pytest.mark.xfail(strict=True, reason="P96-adjacent: the map document has no ceiling yet; D12 adds one")
    def test_it_ships_no_more_pins_than_the_ceiling(self) -> None:
        response = self.client.post(reverse("map.search"), {})

        self.assertEqual(response.status_code, 200, f"map.search returned {response.status_code}")
        pins = _embedded_pins(response.content.decode())
        self.assertLessEqual(
            len(pins),
            TEST_CAP,
            f"map.search embedded {len(pins)} pins against a ceiling of {TEST_CAP}; the document "
            "is O(account), so its cost is set by how many pins the requester owns.",
        )

    @pytest.mark.xfail(strict=True, reason="P96-adjacent: the map document has no ceiling yet; D12 adds one")
    def test_a_truncated_document_says_so(self) -> None:
        """Silently dropping pins would be worse than shipping them all.

        Whatever the client is meant to do next - page through `map.pins`, switch
        to viewport mode - it can only do it if the response admits it is partial.
        """
        response = self.client.post(reverse("map.search"), {})
        body = response.content.decode()

        self.assertTrue(
            any(marker in body for marker in ("truncated", "next_cursor", '"mode"')),
            "the response carries no truncation marker, so a client cannot tell a capped document from a complete one",
        )


class TheSeedIsRealTests(_SeededMapCase):
    """Guards the reproduction itself: these counts must come from real rows.

    Not xfail. If this ever fails, every assertion above is measuring an empty
    page rather than an uncapped one, and their failures would mean nothing.

    It is also a deliberate tripwire on the response *shape*. D12 replaces
    `map/data.html` with a streamed NDJSON document, at which point
    `_embedded_pins` stops finding its script tag and this fails with a message
    saying so - which is the right moment to rewrite this file against the new
    contract, rather than leaving a regex quietly matching nothing.
    """

    def test_the_profile_really_owns_the_pins(self) -> None:
        self.assertEqual(Pin.objects.filter(profile=self.profile).root_pins().count(), SEEDED)

    def test_the_filter_post_really_ships_them_today(self) -> None:
        response = self.client.post(reverse("map.search"), {})

        pins = _embedded_pins(response.content.decode())
        self.assertEqual(
            len(pins),
            SEEDED,
            "the filter POST did not ship every seeded pin, so the ceiling tests above are not "
            "measuring what they claim",
        )
