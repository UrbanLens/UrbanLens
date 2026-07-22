"""Tests for manual sync between a pin's child pins and its wiki's child wikis
(services/controllers pin_wiki_sync).

Two hierarchies can drift apart even when both exist: a hand-placed child pin
nobody's documented on the wiki yet, or a wiki child nobody's personally
pinned. Covers both directions, proximity-based dedup (neither side publishes
a footprint polygon, unlike REData buildings), and that neither direction ever
creates a wiki that doesn't already exist.
"""

from __future__ import annotations

from django.contrib.auth.models import User
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin, PinType
from urbanlens.dashboard.models.wiki.model import Wiki
from urbanlens.dashboard.models.wiki_edit import WikiEdit
from urbanlens.dashboard.services.pin_wiki_sync import pull_children_from_wiki, send_pins_to_wiki

_coord_counter = 0


def _make_location(**kwargs) -> Location:
    global _coord_counter
    _coord_counter += 1
    kwargs.setdefault("latitude", 48.0 + _coord_counter * 0.001)
    kwargs.setdefault("longitude", -91.0 - _coord_counter * 0.001)
    return baker.make(Location, google_place=None, **kwargs)


class SendPinsToWikiTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.profile = baker.make("dashboard.Profile")
        self.location = _make_location()
        self.parent = baker.make(Pin, profile=self.profile, location=self.location, slug="campus")

    def test_no_wiki_creates_nothing(self) -> None:
        child = baker.make(Pin, profile=self.profile, parent_pin=self.parent, location=_make_location(), name="Tool Shed", pin_type=PinType.BUILDING)
        self.assertEqual(send_pins_to_wiki(self.parent, [child], self.profile), 0)
        self.assertFalse(Wiki.objects.filter(location__isnull=False).exclude(location=self.location).exists())

    def test_creates_a_child_wiki_for_each_selected_pin(self) -> None:
        wiki = baker.make(Wiki, location=self.location, name="Campus")
        child = baker.make(Pin, profile=self.profile, parent_pin=self.parent, location=_make_location(), name="Tool Shed", pin_type=PinType.BUILDING)

        created = send_pins_to_wiki(self.parent, [child], self.profile)

        self.assertEqual(created, 1)
        child_wiki = wiki.child_wikis.get()
        self.assertEqual(child_wiki.name, "Tool Shed")
        self.assertEqual(child_wiki.pin_type, PinType.BUILDING)

    def test_a_pin_already_covered_by_a_nearby_child_wiki_is_skipped(self) -> None:
        wiki = baker.make(Wiki, location=self.location, name="Campus")
        loc = baker.make(Location, latitude="48.500000", longitude="-91.500000", google_place=None)
        baker.make(Wiki, location=loc, parent_wiki=wiki, name="Already documented")
        child = baker.make(
            Pin,
            profile=self.profile,
            parent_pin=self.parent,
            location=baker.make(Location, latitude="48.500001", longitude="-91.500001", google_place=None),
            name="Tool Shed",
        )

        created = send_pins_to_wiki(self.parent, [child], self.profile)

        self.assertEqual(created, 0)
        self.assertEqual(wiki.child_wikis.count(), 1)

    def test_only_the_selected_pins_are_sent(self) -> None:
        wiki = baker.make(Wiki, location=self.location, name="Campus")
        selected = baker.make(Pin, profile=self.profile, parent_pin=self.parent, location=_make_location(), name="Selected")
        baker.make(Pin, profile=self.profile, parent_pin=self.parent, location=_make_location(), name="Not selected")

        send_pins_to_wiki(self.parent, [selected], self.profile)

        self.assertEqual(wiki.child_wikis.count(), 1)
        self.assertEqual(wiki.child_wikis.get().name, "Selected")

    def test_one_wiki_edit_covers_the_whole_batch(self) -> None:
        wiki = baker.make(Wiki, location=self.location, name="Campus")
        children = [baker.make(Pin, profile=self.profile, parent_pin=self.parent, location=_make_location(), name=f"Building {i}") for i in range(3)]

        send_pins_to_wiki(self.parent, children, self.profile)

        edits = WikiEdit.objects.filter(wiki=wiki)
        self.assertEqual(edits.count(), 1)
        self.assertIn("child_wikis_imported", edits.first().changes)
        self.assertEqual(edits.first().editor, self.profile)

    def test_an_empty_selection_creates_nothing_or_a_wiki_edit(self) -> None:
        wiki = baker.make(Wiki, location=self.location, name="Campus")
        self.assertEqual(send_pins_to_wiki(self.parent, [], self.profile), 0)
        self.assertFalse(WikiEdit.objects.filter(wiki=wiki).exists())


class PullChildrenFromWikiTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.profile = baker.make("dashboard.Profile")
        self.location = _make_location()
        self.parent = baker.make(Pin, profile=self.profile, location=self.location, slug="campus")

    def test_no_wiki_creates_nothing(self) -> None:
        self.assertEqual(pull_children_from_wiki(self.parent), 0)

    def test_no_child_wikis_creates_nothing(self) -> None:
        baker.make(Wiki, location=self.location, name="Campus")
        self.assertEqual(pull_children_from_wiki(self.parent), 0)

    def test_creates_a_pin_for_each_child_wiki(self) -> None:
        wiki = baker.make(Wiki, location=self.location, name="Campus")
        baker.make(
            Wiki,
            location=baker.make(Location, latitude="48.900000", longitude="-91.900000", google_place=None),
            parent_wiki=wiki,
            name="Powerhouse",
            pin_type=PinType.BUILDING,
        )

        created = pull_children_from_wiki(self.parent)

        self.assertEqual(created, 1)
        child_pin = self.parent.detail_pins.get()
        self.assertEqual(child_pin.name, "Powerhouse")
        self.assertEqual(child_pin.pin_type, PinType.BUILDING)
        self.assertFalse(child_pin.name_is_user_provided)
        self.assertEqual(child_pin.wiki_id, wiki.pk)

    def test_a_child_wiki_already_covered_by_a_nearby_pin_is_skipped(self) -> None:
        wiki = baker.make(Wiki, location=self.location, name="Campus")
        baker.make(
            Wiki,
            location=baker.make(Location, latitude="48.900000", longitude="-91.900000", google_place=None),
            parent_wiki=wiki,
            name="Powerhouse",
        )
        baker.make(
            Pin,
            profile=self.profile,
            parent_pin=self.parent,
            location=baker.make(Location, latitude="48.900001", longitude="-91.900001", google_place=None),
            name="My Powerhouse Pin",
        )

        created = pull_children_from_wiki(self.parent)

        self.assertEqual(created, 0)
        self.assertEqual(self.parent.detail_pins.count(), 1)

    def test_running_twice_does_not_duplicate(self) -> None:
        wiki = baker.make(Wiki, location=self.location, name="Campus")
        baker.make(Wiki, location=baker.make(Location, latitude="48.900000", longitude="-91.900000", google_place=None), parent_wiki=wiki, name="Powerhouse")

        pull_children_from_wiki(self.parent)
        second = pull_children_from_wiki(self.parent)

        self.assertEqual(second, 0)
        self.assertEqual(self.parent.detail_pins.count(), 1)


class SendToWikiViewTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.user = baker.make(User)
        self.client.force_login(self.user)
        self.location = _make_location()
        self.parent = baker.make(Pin, profile=self.user.profile, location=self.location, slug="campus")
        self.url = reverse("pin.detail_pins.send_to_wiki", kwargs={"pin_slug": self.parent.slug})

    def test_no_selection_is_a_bad_request(self) -> None:
        self.assertEqual(self.client.post(self.url).status_code, 400)

    def test_no_wiki_toasts_and_creates_nothing(self) -> None:
        child = baker.make(Pin, profile=self.user.profile, parent_pin=self.parent, location=_make_location())
        response = self.client.post(self.url, {"child_pin_uuids": [str(child.uuid)]})
        self.assertEqual(response.status_code, 200)
        self.assertIn("no community wiki", response["HX-Trigger"])

    def test_sends_the_selected_child_pin(self) -> None:
        wiki = baker.make(Wiki, location=self.location, name="Campus")
        child = baker.make(Pin, profile=self.user.profile, parent_pin=self.parent, location=_make_location(), name="Tool Shed")
        response = self.client.post(self.url, {"child_pin_uuids": [str(child.uuid)]})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(wiki.child_wikis.count(), 1)

    def test_a_uuid_not_belonging_to_this_pins_children_is_ignored(self) -> None:
        baker.make(Wiki, location=self.location, name="Campus")
        other_pin = baker.make(Pin, profile=self.user.profile, location=_make_location())
        response = self.client.post(self.url, {"child_pin_uuids": [str(other_pin.uuid)]})
        self.assertEqual(response.status_code, 200)
        self.assertIn("Nothing to send", response["HX-Trigger"])

    def test_another_users_pin_is_not_reachable(self) -> None:
        other = baker.make(Pin, profile=baker.make(User).profile, location=_make_location(), slug="not-mine")
        response = self.client.post(reverse("pin.detail_pins.send_to_wiki", kwargs={"pin_slug": other.slug}), {"child_pin_uuids": ["x"]})
        self.assertEqual(response.status_code, 404)


class PullFromWikiViewTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.user = baker.make(User)
        self.client.force_login(self.user)
        self.location = _make_location()
        self.parent = baker.make(Pin, profile=self.user.profile, location=self.location, slug="campus")
        self.url = reverse("pin.detail_pins.pull_from_wiki", kwargs={"pin_slug": self.parent.slug})

    def test_no_wiki_toasts_and_creates_nothing(self) -> None:
        response = self.client.post(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertIn("no community wiki", response["HX-Trigger"])

    def test_pulls_child_wikis_into_child_pins(self) -> None:
        wiki = baker.make(Wiki, location=self.location, name="Campus")
        baker.make(Wiki, location=baker.make(Location, latitude="49.100000", longitude="-92.100000", google_place=None), parent_wiki=wiki, name="Powerhouse")
        response = self.client.post(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.parent.detail_pins.count(), 1)
        self.assertIn("pinDetailPinsChanged", response["HX-Trigger"])

    def test_another_users_pin_is_not_reachable(self) -> None:
        other = baker.make(Pin, profile=baker.make(User).profile, location=_make_location(), slug="not-mine")
        self.assertEqual(self.client.post(reverse("pin.detail_pins.pull_from_wiki", kwargs={"pin_slug": other.slug})).status_code, 404)
