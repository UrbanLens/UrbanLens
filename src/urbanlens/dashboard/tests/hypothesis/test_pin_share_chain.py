"""Tests for pin-share lineage tracking and the Memories → Sharing page."""

from __future__ import annotations

from django.contrib.auth.models import User
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.controllers.memories import _SHARE_GROUPS_PER_PAGE
from urbanlens.dashboard.controllers.pin_sharing import _create_pin_from_share
from urbanlens.dashboard.models.friendship.model import Friendship, FriendshipStatus
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.markup.model import MarkupMap
from urbanlens.dashboard.models.markup.share import MarkupMapShare
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.pin_share import PinShare, PinShareOrigin, PinShareStatus


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


class IncomingDetectedShareHidesLivePinTests(_ShareChainTestCase):
    """The received side of the Sharing page must not read the sharer's live pin for a DETECTED-status share (auto-recorded from a shared map, a DM, or a trip activity - see PinShareStatus.DETECTED's docstring: "never actionable"). Unlike an EXPLICIT share awaiting accept/reject, the recipient never consented to see anything about these, so unconditionally reading share.pin/share.place_label - which the goal's own litmus test flags as a live reference - was a real leak."""

    def setUp(self) -> None:
        super().setUp()
        self.pin_a.name = "Sender's Private Cabin"
        self.pin_a.save(update_fields=["name"])

    def _detected_share(self, *, origin: str) -> PinShare:
        return PinShare.objects.create(
            pin=self.pin_a,
            from_profile=self.profiles["a"],
            to_profile=self.profiles["b"],
            origin=origin,
            status=PinShareStatus.DETECTED,
        )

    def test_a_trip_activity_detected_share_does_not_expose_the_pin(self):
        self._detected_share(origin=PinShareOrigin.TRIP_ACTIVITY)
        self.client.force_login(self.users["b"])

        response = self.client.get(reverse("memories.sharing.received"))

        groups = response.context["incoming_share_groups"]
        self.assertEqual(len(groups), 1)
        self.assertIsNone(groups[0]["pin"])
        self.assertNotIn("Sender's Private Cabin", response.content.decode())

    def test_the_sharing_page_itself_never_carries_the_name_either(self):
        # The received half is fetched separately now. Kept as its own
        # assertion so re-inlining it cannot quietly reopen the leak.
        self._detected_share(origin=PinShareOrigin.TRIP_ACTIVITY)
        self.client.force_login(self.users["b"])

        response = self.client.get(reverse("memories.sharing"))

        self.assertEqual(response.status_code, 200)
        self.assertNotIn("Sender's Private Cabin", response.content.decode())

    def test_a_map_detected_share_does_not_expose_the_pin(self):
        self._detected_share(origin=PinShareOrigin.MAP_DETECTED)
        self.client.force_login(self.users["b"])

        response = self.client.get(reverse("memories.sharing.received"))

        groups = response.context["incoming_share_groups"]
        self.assertIsNone(groups[0]["pin"])
        self.assertNotIn("Sender's Private Cabin", response.content.decode())

    def test_the_location_derived_label_is_shown_instead(self):
        self._detected_share(origin=PinShareOrigin.TRIP_ACTIVITY)
        self.client.force_login(self.users["b"])

        response = self.client.get(reverse("memories.sharing.received"))

        groups = response.context["incoming_share_groups"]
        self.assertEqual(groups[0]["place_label"], "Old Mill")

    def test_an_explicit_pending_share_still_shows_the_pin(self):
        """The fix must not hide a share the recipient actually needs to decide on."""
        share = PinShare.objects.create(
            pin=self.pin_a,
            from_profile=self.profiles["a"],
            to_profile=self.profiles["b"],
            status=PinShareStatus.PENDING,
        )
        self.client.force_login(self.users["b"])

        response = self.client.get(reverse("memories.sharing.received"))

        groups = response.context["incoming_share_groups"]
        self.assertEqual(groups[0]["pin"], self.pin_a)
        self.assertIn(share, groups[0]["shares"])
        # Autoescaped output turns the apostrophe into &#x27; - assert on the
        # rendered form, not the raw name.
        self.assertIn("Sender&#x27;s Private Cabin", response.content.decode())

    def test_a_mixed_group_with_one_actionable_share_still_shows_the_pin(self):
        """A DETECTED share for a pin the recipient ALSO has a real (pending/accepted) share
        for must not lose the pin - there's a legitimate share justifying the reveal."""
        PinShare.objects.create(
            pin=self.pin_a,
            from_profile=self.profiles["a"],
            to_profile=self.profiles["b"],
            status=PinShareStatus.PENDING,
        )
        self._detected_share(origin=PinShareOrigin.MAP_DETECTED)
        self.client.force_login(self.users["b"])

        response = self.client.get(reverse("memories.sharing.received"))

        groups = response.context["incoming_share_groups"]
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]["pin"], self.pin_a)
        self.assertEqual(len(groups[0]["shares"]), 2)


class MemoriesSharingMapsPageTests(_ShareChainTestCase):
    """The Sharing page also lists standalone maps shared via MarkupMapShare."""

    def test_page_groups_map_shares_by_map(self):
        markup_map = MarkupMap.objects.create(profile=self.profiles["a"], title="Old Mill route")
        MarkupMapShare.objects.create(
            markup_map=markup_map, from_profile=self.profiles["a"], to_profile=self.profiles["b"]
        )
        MarkupMapShare.objects.create(
            markup_map=markup_map, from_profile=self.profiles["a"], to_profile=self.profiles["c"]
        )
        self.client.force_login(self.users["a"])

        response = self.client.get(reverse("memories.sharing"))

        self.assertEqual(response.status_code, 200)
        groups = response.context["map_share_groups"]
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]["map"], markup_map)
        self.assertEqual(
            {share.to_profile_id for share in groups[0]["shares"]}, {self.profiles["b"].pk, self.profiles["c"].pk}
        )

    def test_page_only_shows_own_map_shares(self):
        markup_map = MarkupMap.objects.create(profile=self.profiles["b"], title="Not mine")
        MarkupMapShare.objects.create(
            markup_map=markup_map, from_profile=self.profiles["b"], to_profile=self.profiles["c"]
        )
        self.client.force_login(self.users["a"])

        response = self.client.get(reverse("memories.sharing"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["map_share_groups"], [])


class SharingPagePaginationTests(_ShareChainTestCase):
    """P69: the Sharing page rendered both halves of its toggle, unbounded.

    The two halves are a client-side toggle, so the received half was queried, grouped and rendered on every
    load for a panel nobody had opened - and neither half had a slice, so both grew with the account."""

    def _pins_shared_to_b(self, count: int) -> list[Pin]:
        made = []
        for index in range(count):
            location = baker.make(
                Location,
                latitude=f"42.{index + 200:06d}"[:9],
                longitude="-73.900000",
                official_name=f"Place {index:03d}",
            )
            pin = Pin.objects.create(profile=self.profiles["a"], location=location)
            PinShare.objects.create(
                pin=pin, from_profile=self.profiles["a"], to_profile=self.profiles["b"], status=PinShareStatus.PENDING
            )
            made.append(pin)
        return made

    def test_the_page_renders_one_page_of_sent_groups(self):
        self._pins_shared_to_b(_SHARE_GROUPS_PER_PAGE + 3)
        self.client.force_login(self.users["a"])

        response = self.client.get(reverse("memories.sharing"))

        self.assertEqual(len(response.context["share_groups"]), _SHARE_GROUPS_PER_PAGE)
        self.assertEqual(response.context["sent_pins_page_obj"].paginator.count, _SHARE_GROUPS_PER_PAGE + 3)

    def test_the_rest_are_on_the_next_page(self):
        self._pins_shared_to_b(_SHARE_GROUPS_PER_PAGE + 3)
        self.client.force_login(self.users["a"])

        response = self.client.get(reverse("memories.sharing.sent"), {"sent_pins_page": 2})

        self.assertEqual(len(response.context["share_groups"]), 3)

    def test_the_page_does_not_render_the_received_half(self):
        self._pins_shared_to_b(2)
        self.client.force_login(self.users["b"])

        response = self.client.get(reverse("memories.sharing"))

        self.assertEqual(response.status_code, 200)
        self.assertNotIn("incoming_share_groups", response.context)
        # It is fetched by the toggle button instead.
        self.assertContains(response, reverse("memories.sharing.received"))

    def test_the_toggle_still_names_the_totals_not_the_page(self):
        # The counts gate the empty state and label both buttons, so they have
        # to survive the lists being sliced.
        self._pins_shared_to_b(_SHARE_GROUPS_PER_PAGE + 3)
        self.client.force_login(self.users["a"])

        response = self.client.get(reverse("memories.sharing"))

        self.assertEqual(response.context["sent_count"], _SHARE_GROUPS_PER_PAGE + 3)
        self.assertTrue(response.context["has_any_shares"])

    def test_groups_are_ordered_by_their_most_recent_share(self):
        pins = self._pins_shared_to_b(3)
        # Reshare the oldest place, which should pull it back to the top.
        PinShare.objects.create(
            pin=pins[0], from_profile=self.profiles["a"], to_profile=self.profiles["c"], status=PinShareStatus.PENDING
        )
        self.client.force_login(self.users["a"])

        response = self.client.get(reverse("memories.sharing"))

        groups = response.context["share_groups"]
        self.assertEqual(groups[0]["pin"], pins[0])

    def test_a_share_with_no_pin_groups_by_its_location(self):
        # Coordinates typed into a DM the sender never pinned. Without their own
        # group key these all collapse into one bucket.
        for index in range(2):
            location = baker.make(
                Location,
                latitude="42.200000",
                longitude=f"-73.{index + 800:06d}"[:10],
                official_name=f"Unpinned {index}",
            )
            PinShare.objects.create(
                pin=None,
                location=location,
                from_profile=self.profiles["a"],
                to_profile=self.profiles["b"],
                status=PinShareStatus.PENDING,
            )
        self.client.force_login(self.users["a"])

        response = self.client.get(reverse("memories.sharing"))

        self.assertEqual(len(response.context["share_groups"]), 2)

    def test_the_received_partial_pages_on_its_own_parameter(self):
        # Four lists render across this page; a shared `page` would move them
        # all with one click.
        self._pins_shared_to_b(_SHARE_GROUPS_PER_PAGE + 3)
        self.client.force_login(self.users["b"])

        response = self.client.get(reverse("memories.sharing.received"), {"received_pins_page": 2})

        self.assertEqual(len(response.context["incoming_share_groups"]), 3)
        self.assertContains(response, "received_pins_page=1")
