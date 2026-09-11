"""The filter panel must not re-send pins the asker already has.

`map.search` fires on every filter change - a slider drag is several - and its
answer was every matching pin's full payload. For a 10,000-pin account that is an
11.45MB document per change, built from the database each time, on a request the
user is waiting on. Nothing capped it, so the cost of one press was set by how
many pins the presser owned, which is the shape this programme exists to remove.

The map page has already loaded every one of those pins into its own store. The
part of the answer it cannot derive is *which* matched, so that is what the
response should carry: identifiers, one `values_list`, no payload build.

The substitution is only sound while the client's store is the current pin set,
so these tests hold the two halves of that:

* identifiers are served **only** against a fingerprint that is still current,
  and the value the client sends must be the one `map.pins.meta` gave it - if the
  two ever derive it differently, the feature quietly stops engaging (or, worse,
  engages while the store is stale) and every other test here would still pass;
* everything else - no claim, a stale claim, a client that never loaded the whole
  account - still gets payloads, bounded by the response ceiling.
"""

from __future__ import annotations

import json
import re
from typing import Any

from django.contrib.auth.models import User
from django.test import override_settings
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.instantiation_scaling import count_instantiations
from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.services.map_pins.filter_results import STORE_FINGERPRINT_FIELD
from urbanlens.dashboard.services.map_pins.fingerprint import pin_collection_state

SEEDED = 6

_PINS_SCRIPT = re.compile(r'<script[^>]*id="map-filter-pins"[^>]*>(.*?)</script>', re.DOTALL)
_UUIDS_SCRIPT = re.compile(r'<script[^>]*id="map-filter-uuids"[^>]*>(.*?)</script>', re.DOTALL)


def _embedded(pattern: re.Pattern[str], html: str) -> Any:
    """Parse one of the two documents `map.search` can answer with.

    Args:
        pattern: Which script tag to look for.
        html: The rendered response body.

    Returns:
        The parsed value, or None when the response is not of that kind.
    """
    match = pattern.search(html)
    return None if match is None else json.loads(match.group(1))


class _FilterCase(TestCase):
    """A logged-in profile with a handful of root pins."""

    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # absorbs the bootstrap site-admin promotion
        self.user = baker.make(User)
        self.profile: Profile = self.user.profile
        self.client.force_login(self.user)
        for index in range(SEEDED):
            location = baker.make(
                Location,
                latitude=f"38.{index + 1:06d}",
                longitude=f"-77.{index + 1:06d}",
                official_name="Store Place",
            )
            baker.make(Pin, profile=self.profile, location=location)

    def current_fingerprint(self) -> str:
        """The value a client that just polled `map.pins.meta` would hold."""
        return pin_collection_state(self.profile).fingerprint

    def search(self, **extra: str) -> Any:
        """POST the filter form with no criteria, so everything matches."""
        return self.client.post(reverse("map.search"), extra)


class TheClientStoreIsUsedWhenItIsCurrentTests(_FilterCase):
    """The whole point: an asker who has the pins is sent identifiers."""

    def test_a_current_fingerprint_is_answered_with_identifiers(self) -> None:
        response = self.search(**{STORE_FINGERPRINT_FIELD: self.current_fingerprint()})

        self.assertEqual(response.status_code, 200)
        body = response.content.decode()
        uuids = _embedded(_UUIDS_SCRIPT, body)
        self.assertIsNotNone(uuids, "a current client store was still answered with payloads")
        self.assertEqual(sorted(uuids or []), sorted(str(pin.uuid) for pin in Pin.objects.filter(profile=self.profile)))

    def test_the_identifier_answer_carries_no_payloads(self) -> None:
        """Otherwise the saving is notional - the build is the expensive half."""
        body = self.search(**{STORE_FINGERPRINT_FIELD: self.current_fingerprint()}).content.decode()

        self.assertIsNone(_embedded(_PINS_SCRIPT, body), "the identifier response shipped payloads as well")

    def test_the_identifier_answer_does_not_grow_with_the_account(self) -> None:
        """The cost being removed is the model graph, not only the bytes.

        Stated as a slope rather than an absolute: the form has its own fixed
        setup cost, and an absolute bound would be a number to re-tune rather
        than the property that matters, which is that answering does not get
        more expensive as the asker accumulates pins.
        """
        with count_instantiations() as small:
            self.search(**{STORE_FINGERPRINT_FIELD: self.current_fingerprint()})
        for index in range(SEEDED * 3):
            location = baker.make(
                Location,
                latitude=f"41.{index + 1:06d}",
                longitude=f"-74.{index + 1:06d}",
                official_name="Growth Place",
            )
            baker.make(Pin, profile=self.profile, location=location)
        with count_instantiations() as large:
            self.search(**{STORE_FINGERPRINT_FIELD: self.current_fingerprint()})

        self.assertLessEqual(
            large.total,
            small.total,
            f"answering got more expensive as the account grew: {small.total} -> {large.total} "
            f"objects ({dict(large.by_model)})",
        )

    def test_the_fingerprint_the_client_sends_is_the_one_meta_serves(self) -> None:
        """Guards the handshake itself, across the two endpoints that share it.

        The client reads this value from `map.pins.meta` and posts it here. Were
        the two to derive it differently, identifiers would never be served and
        every other test in this class would still pass, because they take the
        value from the same helper the view does.
        """
        served = json.loads(self.client.get(reverse("map.pins.meta")).content)["fingerprint"]

        body = self.search(**{STORE_FINGERPRINT_FIELD: served}).content.decode()
        self.assertIsNotNone(
            _embedded(_UUIDS_SCRIPT, body),
            "the fingerprint map.pins.meta serves did not unlock the identifier response",
        )


class AnUntrustedStoreStillGetsPayloadsTests(_FilterCase):
    """Every path that cannot prove the store is current must fall back."""

    def test_no_claim_is_answered_with_payloads(self) -> None:
        body = self.search().content.decode()

        self.assertIsNotNone(_embedded(_PINS_SCRIPT, body), "a client making no claim was not sent payloads")

    def test_a_stale_claim_is_answered_with_payloads(self) -> None:
        stale = self.current_fingerprint()
        location = baker.make(Location, latitude="39.500000", longitude="-76.500000", official_name="Later Place")
        baker.make(Pin, profile=self.profile, location=location)

        body = self.search(**{STORE_FINGERPRINT_FIELD: stale}).content.decode()

        self.assertIsNone(_embedded(_UUIDS_SCRIPT, body), "a stale store was trusted")
        self.assertIsNotNone(_embedded(_PINS_SCRIPT, body))

    def test_another_profiles_fingerprint_is_not_a_claim_on_this_one(self) -> None:
        """The value is opaque, but it is not a secret and it is not scoped."""
        other = baker.make(User)
        for index in range(SEEDED):
            location = baker.make(
                Location,
                latitude=f"40.{index + 1:06d}",
                longitude=f"-75.{index + 1:06d}",
                official_name="Other Place",
            )
            baker.make(Pin, profile=other.profile, location=location)

        body = self.search(
            **{STORE_FINGERPRINT_FIELD: pin_collection_state(other.profile).fingerprint}
        ).content.decode()

        self.assertIsNone(
            _embedded(_UUIDS_SCRIPT, body), "another profile's fingerprint unlocked the identifier response"
        )


@override_settings(MAP_DOCUMENT_MAX_PINS=2)
class TheCeilingBoundsBothAnswersTests(_FilterCase):
    """A ceiling that only one of the two transports respects is not a ceiling."""

    def test_the_identifier_answer_is_bounded(self) -> None:
        uuids = _embedded(
            _UUIDS_SCRIPT, self.search(**{STORE_FINGERPRINT_FIELD: self.current_fingerprint()}).content.decode()
        )

        self.assertIsNotNone(uuids)
        self.assertLessEqual(len(uuids or []), 2)

    def test_the_payload_answer_is_bounded(self) -> None:
        pins = _embedded(_PINS_SCRIPT, self.search().content.decode())

        self.assertIsNotNone(pins)
        self.assertLessEqual(len(pins or []), 2)

    def test_a_bounded_answer_says_how_many_it_left_out(self) -> None:
        """A client that cannot tell a partial answer from a complete one will
        show the partial one as the whole truth."""
        for body in (
            self.search().content.decode(),
            self.search(**{STORE_FINGERPRINT_FIELD: self.current_fingerprint()}).content.decode(),
        ):
            self.assertIn("map-filter-meta", body)
            meta = _embedded(re.compile(r'<script[^>]*id="map-filter-meta"[^>]*>(.*?)</script>', re.DOTALL), body)
            self.assertTrue(meta["truncated"], "a capped response did not admit it was capped")
            self.assertEqual(meta["total"], SEEDED, "a capped response did not say how many matched")
