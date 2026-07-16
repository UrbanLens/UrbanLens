"""Tests for coordinate/address detection in direct messages.

Parsing (pure, no DB): every supported coordinate format resolves to the same
place, prose numbers don't false-positive, and a hypothesis round-trip checks
arbitrary in-range decimal pairs.

Recording (DB): a plaintext message with coordinates creates a mention + a
DM_DETECTED share + the recipient's exposure; a recipient who already has the
place pinned gets a reference-only mention (no share - it doesn't count);
encrypted messages are never scanned; "Add to map" materializes the pin; and
onward shares chain back to the DM share.
"""

from __future__ import annotations

from django.contrib.auth.models import User
from django.urls import reverse
from hypothesis import given
from hypothesis import strategies as st
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.direct_messages.location_mention import DirectMessageLocationMention
from urbanlens.dashboard.models.direct_messages.model import DirectMessage
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.pin_share import LocationExposure, PinShare, PinShareOrigin, PinShareStatus
from urbanlens.dashboard.services.dm_location_detection import (
    detect_coordinate_mentions,
    parse_addresses,
    parse_coordinates,
)


class ParseCoordinatesTests(TestCase):
    """parse_coordinates handles every supported format (pure, no DB)."""

    databases = []

    def _single(self, text: str) -> tuple[float, float]:
        matches = parse_coordinates(text)
        self.assertEqual(len(matches), 1, f"expected one match in {text!r}, got {matches}")
        return matches[0].latitude, matches[0].longitude

    def test_decimal_pair(self):
        self.assertEqual(self._single("meet me at 40.7128, -74.0060 tonight"), (40.7128, -74.006))

    def test_decimal_pair_without_spaces(self):
        self.assertEqual(self._single("40.7128,-74.0060"), (40.7128, -74.006))

    def test_hemisphere_suffixed_decimals(self):
        latitude, longitude = self._single("it's at 40.7128 N, 74.0060 W")
        self.assertAlmostEqual(latitude, 40.7128)
        self.assertAlmostEqual(longitude, -74.006)

    def test_dms(self):
        latitude, longitude = self._single("40°42'46\"N 74°00'22\"W")
        self.assertAlmostEqual(latitude, 40.712778, places=5)
        self.assertAlmostEqual(longitude, -74.006111, places=5)

    def test_degrees_decimal_minutes(self):
        latitude, longitude = self._single("40°42.767'N, 74°0.367'W")
        self.assertAlmostEqual(latitude, 40.712783, places=5)
        self.assertAlmostEqual(longitude, -74.006117, places=5)

    def test_google_maps_url(self):
        self.assertEqual(self._single("https://www.google.com/maps/@40.7128,-74.0060,17z"), (40.7128, -74.006))

    def test_google_maps_query_url(self):
        self.assertEqual(self._single("https://maps.google.com/?q=40.7128,-74.0060"), (40.7128, -74.006))

    def test_geo_uri(self):
        self.assertEqual(self._single("geo:40.7128,-74.0060"), (40.7128, -74.006))

    def test_prose_numbers_do_not_match(self):
        self.assertEqual(parse_coordinates("meet at 7, 8 of us are coming"), [])
        self.assertEqual(parse_coordinates("that costs 12.99, 24.99 for two"), [])
        self.assertEqual(parse_coordinates("no numbers here at all"), [])

    def test_out_of_range_rejected(self):
        self.assertEqual(parse_coordinates("99.123456, -200.654321"), [])

    def test_null_island_rejected(self):
        self.assertEqual(parse_coordinates("0.000000, 0.000000"), [])

    def test_duplicate_pairs_collapse(self):
        matches = parse_coordinates("40.7128, -74.0060 and again 40.7128, -74.0060")
        self.assertEqual(len(matches), 1)

    def test_multiple_distinct_pairs(self):
        matches = parse_coordinates("40.7128, -74.0060 then 42.6526, -73.7562")
        self.assertEqual(len(matches), 2)

    def test_mention_cap(self):
        text = " ".join(f"41.{i}00001, -73.{i}00001" for i in range(1, 9))
        self.assertEqual(len(parse_coordinates(text)), 5)

    @given(
        st.floats(min_value=-89.9, max_value=89.9).filter(lambda v: abs(v) > 0.001),
        st.floats(min_value=-179.9, max_value=179.9).filter(lambda v: abs(v) > 0.001),
    )
    def test_decimal_roundtrip(self, latitude: float, longitude: float):
        latitude, longitude = round(latitude, 6), round(longitude, 6)
        text = f"check out {latitude:.6f}, {longitude:.6f} sometime"
        matches = parse_coordinates(text)
        assert len(matches) == 1
        assert matches[0].latitude == latitude
        assert matches[0].longitude == longitude


class ParseAddressesTests(TestCase):
    """parse_addresses finds street-address-shaped text (pure, no DB)."""

    databases = []

    def test_simple_street_address(self):
        self.assertEqual(parse_addresses("it's at 123 Main St"), ["123 Main St"])

    def test_address_with_city_state(self):
        results = parse_addresses("try 4287 Buckingham Pond Rd, Albany, NY 12203 after dark")
        self.assertEqual(results, ["4287 Buckingham Pond Rd, Albany, NY 12203"])

    def test_multi_word_street(self):
        self.assertEqual(parse_addresses("930 North Broadway Avenue is the one"), ["930 North Broadway Avenue"])

    def test_prose_does_not_match(self):
        self.assertEqual(parse_addresses("I walked 5 miles down the road yesterday"), [])
        self.assertEqual(parse_addresses("we should hang out sometime"), [])

    def test_duplicates_collapse(self):
        self.assertEqual(len(parse_addresses("123 Main St or 123 main st")), 1)


class _DmDetectionTestCase(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.users = {name: baker.make(User, username=name) for name in ("sender", "recipient", "third")}
        self.profiles = {name: user.profile for name, user in self.users.items()}

    def _message(self, body: str, *, sender: str = "sender", recipient: str = "recipient") -> DirectMessage:
        return DirectMessage.objects.create(sender=self.profiles[sender], recipient=self.profiles[recipient], body=body)


class DetectCoordinateMentionTests(_DmDetectionTestCase):
    """Coordinates in a plaintext DM become a mention + share + exposure."""

    def test_coordinates_create_mention_share_and_exposure(self):
        message = self._message("meet me at 42.100000, -73.900000")
        mentions = detect_coordinate_mentions(message)

        self.assertEqual(len(mentions), 1)
        share = mentions[0].pin_share
        self.assertIsNotNone(share)
        self.assertEqual(share.origin, PinShareOrigin.DM_DETECTED)
        self.assertEqual(share.status, PinShareStatus.PENDING)
        self.assertEqual(share.from_profile_id, self.profiles["sender"].pk)
        self.assertEqual(share.to_profile_id, self.profiles["recipient"].pk)
        self.assertIsNone(share.pin)
        self.assertEqual(share.detected_via_message_id, message.pk)
        self.assertTrue(LocationExposure.objects.filter(profile=self.profiles["recipient"], share=share).exists())

    def test_sender_pin_attached_when_they_have_one(self):
        location = baker.make(Location, latitude="42.100000", longitude="-73.900000")
        sender_pin = Pin.objects.create(profile=self.profiles["sender"], location=location)
        message = self._message("42.100000, -73.900000")
        mentions = detect_coordinate_mentions(message)
        self.assertEqual(mentions[0].pin_share.pin_id, sender_pin.pk)

    def test_recipient_with_pin_gets_reference_only(self):
        location = baker.make(Location, latitude="42.100000", longitude="-73.900000")
        recipient_pin = Pin.objects.create(profile=self.profiles["recipient"], location=location, name="My Secret Mill")
        message = self._message("42.100000, -73.900000")
        mentions = detect_coordinate_mentions(message)

        self.assertEqual(len(mentions), 1)
        self.assertIsNone(mentions[0].pin_share)  # doesn't count as a share
        self.assertFalse(PinShare.objects.filter(to_profile=self.profiles["recipient"]).exists())
        self.assertFalse(LocationExposure.objects.filter(profile=self.profiles["recipient"]).exists())
        self.assertEqual(mentions[0].recipient_pin(), recipient_pin)

    def test_encrypted_message_never_scanned(self):
        message = DirectMessage.objects.create(
            sender=self.profiles["sender"],
            recipient=self.profiles["recipient"],
            body="",
            ciphertext="b2s=",
            nonce="bm9uY2U=",
            key_version=1,
        )
        self.assertEqual(detect_coordinate_mentions(message), [])
        self.assertFalse(DirectMessageLocationMention.objects.exists())

    def test_repeat_message_does_not_double_count(self):
        first = self._message("42.100000, -73.900000")
        detect_coordinate_mentions(first)
        second = self._message("still there? 42.100000, -73.900000")
        mentions = detect_coordinate_mentions(second)

        # The second message gets its own mention (so its footer renders),
        # but no second share/exposure - the place was already shared.
        self.assertEqual(len(mentions), 1)
        self.assertEqual(PinShare.objects.filter(to_profile=self.profiles["recipient"]).count(), 1)
        self.assertEqual(LocationExposure.objects.filter(profile=self.profiles["recipient"]).count(), 1)

    def test_onward_share_chains_back_to_dm_share(self):
        message = self._message("42.100000, -73.900000")
        mentions = detect_coordinate_mentions(message)
        dm_share = mentions[0].pin_share

        location = Location.objects.get(latitude="42.100000", longitude="-73.900000")
        recipient_pin = Pin.objects.create(profile=self.profiles["recipient"], location=location)
        from urbanlens.dashboard.services.share_provenance import resolve_origin_share

        parent = resolve_origin_share(self.profiles["recipient"].pk, pin=recipient_pin)
        self.assertEqual(parent, dm_share)


class MessageMentionAddPinViewTests(_DmDetectionTestCase):
    """The "Add to map" button materializes the recipient's pin from the share."""

    def _mention(self) -> DirectMessageLocationMention:
        message = self._message("42.100000, -73.900000")
        return detect_coordinate_mentions(message)[0]

    def test_add_pin_creates_pin_with_source_share(self):
        mention = self._mention()
        self.client.force_login(self.users["recipient"])

        response = self.client.post(
            reverse("messages.mention.add_pin", kwargs={"profile_slug": self.profiles["sender"].ensure_slug(), "mention_id": mention.pk}),
        )

        self.assertEqual(response.status_code, 200)
        pin = Pin.objects.get(profile=self.profiles["recipient"])
        self.assertEqual(pin.source_share_id, mention.pin_share_id)
        mention.pin_share.refresh_from_db()
        self.assertEqual(mention.pin_share.status, PinShareStatus.ACCEPTED)

    def test_sender_cannot_use_recipient_endpoint(self):
        mention = self._mention()
        self.client.force_login(self.users["sender"])

        response = self.client.post(
            reverse("messages.mention.add_pin", kwargs={"profile_slug": self.profiles["recipient"].ensure_slug(), "mention_id": mention.pk}),
        )

        self.assertEqual(response.status_code, 404)
        self.assertFalse(Pin.objects.filter(profile=self.profiles["recipient"]).exists())

    def test_third_party_cannot_touch_mention(self):
        mention = self._mention()
        self.client.force_login(self.users["third"])

        response = self.client.post(
            reverse("messages.mention.add_pin", kwargs={"profile_slug": self.profiles["sender"].ensure_slug(), "mention_id": mention.pk}),
        )

        self.assertEqual(response.status_code, 404)
