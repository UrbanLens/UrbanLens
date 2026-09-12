"""The map's whole-account endpoints must have a ceiling the account cannot raise."""

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
from urbanlens.dashboard.services.map_pins import document as map_document

#: The Django setting the ceiling lives behind. `UL_MAP_DOCUMENT_MAX_PINS` is the
#: environment variable that feeds it and is not what `override_settings` takes.
SETTING_NAME = "MAP_DOCUMENT_MAX_PINS"

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
        AssertionError: The document is missing, which would make every count assertion below pass without measuring anything."""
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
    """Before anything can be bounded, something has to name the bound.

    Not xfail: D12 landed this half. It stays as the tripwire for the name, which
    is what the mis-wired first draft of this file got wrong.
    """

    def test_a_setting_names_the_maximum_pins_in_one_document(self) -> None:
        self.assertTrue(
            hasattr(settings, SETTING_NAME),
            f"{SETTING_NAME} does not exist, so nothing bounds a whole-account map document - "
            "and an override_settings of that name would silently configure a ceiling no code reads.",
        )

    def test_the_name_is_one_production_reads(self) -> None:
        """Overriding it must move the number the serving code actually asks for."""
        with override_settings(**{SETTING_NAME: 7}):
            self.assertEqual(
                map_document.max_pins(),
                7,
                f"overriding {SETTING_NAME} did not change document.max_pins(), so every ceiling "
                "assertion in this file would be configuring something nothing reads.",
            )


@override_settings(**{SETTING_NAME: TEST_CAP})
class TheFilterPostMustRespectItTests(_SeededMapCase):
    """`map.search` is the one a user triggers repeatedly, by changing a filter."""

    @pytest.mark.xfail(
        strict=True, reason="map.search still ships the whole account; D12 capped map.document and left this one"
    )
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

    @pytest.mark.xfail(
        strict=True, reason="map.search still ships the whole account; D12 capped map.document and left this one"
    )
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

    Not xfail."""

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
