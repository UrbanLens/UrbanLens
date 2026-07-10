"""Tests for pin-share lineage tracking and the Memories → Sharing page.

Covers:
- PinShare.chain_share_count - transitive reshare counting (the spec example:
  A→B, B→C and B→D, D→E and D→F counts 5 shares for A's pin)
- _create_pin_from_share - stamps the new pin's source_share
- PinShareCreateView - links a reshare to the share the pin came from
- MemoriesSharingView - groups shares by pin with chain totals
"""

from __future__ import annotations

from django.contrib.auth.models import User
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.controllers.pin_sharing import _create_pin_from_share
from urbanlens.dashboard.models.friendship.model import Friendship, FriendshipStatus
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.pin_share import PinShare, PinShareStatus


def _befriend(a, b) -> None:
    Friendship.objects.create(from_profile=a, to_profile=b, status=FriendshipStatus.ACCEPTED)


class _ShareChainTestCase(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.users = {name: baker.make(User, username=name) for name in "abcdef"}
        self.profiles = {name: user.profile for name, user in self.users.items()}
        self.location = baker.make(Location, latitude="42.100000", longitude="-73.900000", official_name="Old Mill")
        self.pin_a = Pin.objects.create(profile=self.profiles["a"], location=self.location)

    def _share(self, pin: Pin, from_name: str, to_name: str) -> PinShare:
        """Create a share the way PinShareCreateView does, then accept it."""
        share = PinShare.objects.create(
            pin=pin,
            from_profile=self.profiles[from_name],
            to_profile=self.profiles[to_name],
            parent_share_id=pin.source_share_id,
            status=PinShareStatus.PENDING,
        )
        return share


class ChainShareCountTests(_ShareChainTestCase):
    """chain_share_count follows reshares all the way down."""

    def test_single_share_counts_one(self):
        share = self._share(self.pin_a, "a", "b")
        self.assertEqual(PinShare.chain_share_count([share.pk]), 1)

    def test_spec_example_counts_five(self):
        # A shares with B.
        share_ab = self._share(self.pin_a, "a", "b")
        pin_b = _create_pin_from_share(share_ab)
        # B shares with C and D.
        self._share(pin_b, "b", "c")
        share_bd = self._share(pin_b, "b", "d")
        pin_d = _create_pin_from_share(share_bd)
        # D shares with E and F.
        self._share(pin_d, "d", "e")
        self._share(pin_d, "d", "f")

        self.assertEqual(PinShare.chain_share_count([share_ab.pk]), 5)

    def test_mid_chain_share_counts_its_own_subtree(self):
        share_ab = self._share(self.pin_a, "a", "b")
        pin_b = _create_pin_from_share(share_ab)
        share_bd = self._share(pin_b, "b", "d")
        pin_d = _create_pin_from_share(share_bd)
        self._share(pin_d, "d", "e")
        self._share(pin_d, "d", "f")

        # B's share of the pin: itself + D's two reshares.
        self.assertEqual(PinShare.chain_share_count([share_bd.pk]), 3)

    def test_empty_roots_count_zero(self):
        self.assertEqual(PinShare.chain_share_count([]), 0)


class SourceShareTests(_ShareChainTestCase):
    """Accepting a share stamps the created pin with its source share."""

    def test_created_pin_records_source_share(self):
        share = self._share(self.pin_a, "a", "b")
        new_pin = _create_pin_from_share(share)
        self.assertEqual(new_pin.source_share_id, share.pk)

    def test_reshare_view_links_parent_share(self):
        share_ab = self._share(self.pin_a, "a", "b")
        pin_b = _create_pin_from_share(share_ab)
        _befriend(self.profiles["b"], self.profiles["c"])
        self.client.force_login(self.users["b"])

        response = self.client.post(
            reverse("pin.share.send", kwargs={"pin_slug": pin_b.slug}),
            {"profile_id": self.profiles["c"].pk},
        )

        self.assertEqual(response.status_code, 200)
        reshare = PinShare.objects.get(pin=pin_b, to_profile=self.profiles["c"])
        self.assertEqual(reshare.parent_share_id, share_ab.pk)

    def test_original_share_has_no_parent(self):
        _befriend(self.profiles["a"], self.profiles["b"])
        self.client.force_login(self.users["a"])

        response = self.client.post(
            reverse("pin.share.send", kwargs={"pin_slug": self.pin_a.slug}),
            {"profile_id": self.profiles["b"].pk},
        )

        self.assertEqual(response.status_code, 200)
        share = PinShare.objects.get(pin=self.pin_a, to_profile=self.profiles["b"])
        self.assertIsNone(share.parent_share_id)


class MemoriesSharingPageTests(_ShareChainTestCase):
    """The Sharing page lists shared pins with recipients and chain totals."""

    def test_page_renders_with_chain_counts(self):
        share_ab = self._share(self.pin_a, "a", "b")
        pin_b = _create_pin_from_share(share_ab)
        self._share(pin_b, "b", "c")
        self.client.force_login(self.users["a"])

        response = self.client.get(reverse("memories.sharing"))

        self.assertEqual(response.status_code, 200)
        groups = response.context["share_groups"]
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]["chain_total"], 2)
        self.assertEqual(groups[0]["reshare_count"], 1)

    def test_page_empty_state(self):
        self.client.force_login(self.users["a"])
        response = self.client.get(reverse("memories.sharing"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["share_groups"], [])
