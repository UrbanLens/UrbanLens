"""Positive controls for the fourth concealment review round.

The third review round's V4 finding was that the whole render-test suite had
no positive control for automatic content: `resolve_fields` could be deleted
outright and every test would still pass, because nothing proved the
own/friend/automatic logic ever actually ran rather than happening to agree
with a wholesale-hide default. This file exists so the same thing can't
happen to the surfaces the fourth round added or fixed - each test below
would fail if the code under test were deleted or reverted to its prior
(wholesale-hide, or unguarded) behaviour, not just if concealment broke
outright.

Covers: own+friends visibility for CustomLayer/MapImageOverlay/Album/Reaction/
WikiOwner/WikiPropertySale; the layer_uuid read-side nulling and the write-
safety fix that stops an edit to an unrelated field silently destroying a
real, invisible layer assignment; the WikiOwner/WikiPropertySale dedup fix
(an oracle and, for sales, a direct name leak); wiki-scoped boundary
concealment and the write-path fix that stops a viewer's own just-drawn
boundary vanishing from the very response that saved it; and the search
result display fix (a surviving candidate's title/snippet must come from the
concealed values, not the live row).
"""

from __future__ import annotations

import json
from unittest import mock

from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.abstract.choices import SecurityLevel
from urbanlens.dashboard.models.friendship.model import Friendship, FriendshipStatus
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.markup.model import CustomLayer, PinMarkup
from urbanlens.dashboard.models.property_owner.model import WikiOwner, WikiPropertySale
from urbanlens.dashboard.models.wiki.model import Wiki
from urbanlens.dashboard.services.wiki.concealment import visible_rows

_CONCEALED = mock.patch("urbanlens.dashboard.services.wiki.concealment.concealment_active", return_value=True)


def _wiki_with_viewer_friend_stranger():
    """A wiki plus a viewer (pinned, so they can reach it), an accepted friend, and a stranger."""
    location = baker.make(Location)
    wiki = baker.make(Wiki, location=location, name="Old Mill")
    viewer = baker.make("auth.User").profile
    friend = baker.make("auth.User").profile
    stranger = baker.make("auth.User").profile
    baker.make("dashboard.Pin", profile=viewer, location=location)
    baker.make(Friendship, from_profile=viewer, to_profile=friend, status=FriendshipStatus.ACCEPTED)
    return wiki, viewer, friend, stranger


class OwnContributionVisibleRowsTests(TestCase):
    """Own+friends survive concealment for the models the fourth round moved off wholesale-hide."""

    def setUp(self) -> None:
        super().setUp()
        self.wiki, self.viewer, self.friend, self.stranger = _wiki_with_viewer_friend_stranger()

    def test_custom_layers(self) -> None:
        own = baker.make(CustomLayer, parent_wiki=self.wiki, profile=self.viewer, name="OWN")
        friend_layer = baker.make(CustomLayer, parent_wiki=self.wiki, profile=self.friend, name="FRIEND")
        baker.make(CustomLayer, parent_wiki=self.wiki, profile=self.stranger, name="STRANGER")
        with _CONCEALED:
            visible = visible_rows(CustomLayer.objects.for_wiki(self.wiki), self.wiki, self.viewer)
        self.assertEqual({layer.pk for layer in visible}, {own.pk, friend_layer.pk})

    def test_map_overlays(self) -> None:
        from urbanlens.dashboard.models.map_overlay.model import MapImageOverlay

        corners = {f"{d}_{axis}": 0.0 for d in ("nw", "ne", "se", "sw") for axis in ("latitude", "longitude")}
        own = baker.make(MapImageOverlay, parent_wiki=self.wiki, profile=self.viewer, **corners)
        friend_overlay = baker.make(MapImageOverlay, parent_wiki=self.wiki, profile=self.friend, **corners)
        baker.make(MapImageOverlay, parent_wiki=self.wiki, profile=self.stranger, **corners)
        with _CONCEALED:
            visible = visible_rows(MapImageOverlay.objects.filter(parent_wiki=self.wiki), self.wiki, self.viewer)
        self.assertEqual({o.pk for o in visible}, {own.pk, friend_overlay.pk})

    def test_albums(self) -> None:
        from urbanlens.dashboard.models.album.model import Album

        own = baker.make(Album, parent_wiki=self.wiki, profile=self.viewer, name="OWN")
        friend_album = baker.make(Album, parent_wiki=self.wiki, profile=self.friend, name="FRIEND")
        baker.make(Album, parent_wiki=self.wiki, profile=self.stranger, name="STRANGER")
        with _CONCEALED:
            visible = visible_rows(Album.objects.filter(parent_wiki=self.wiki), self.wiki, self.viewer)
        self.assertEqual({a.pk for a in visible}, {own.pk, friend_album.pk})

    def test_wiki_owner_and_property_sale(self) -> None:
        own = WikiOwner.objects.create(name="Own Owner", created_by=self.viewer)
        own.locations.add(self.wiki.location)
        friend_owner = WikiOwner.objects.create(name="Friend Owner", created_by=self.friend)
        friend_owner.locations.add(self.wiki.location)
        stranger_owner = WikiOwner.objects.create(name="Stranger Owner", created_by=self.stranger)
        stranger_owner.locations.add(self.wiki.location)

        with _CONCEALED:
            visible = visible_rows(WikiOwner.objects.for_location(self.wiki.location), self.wiki, self.viewer)
        self.assertEqual({o.pk for o in visible}, {own.pk, friend_owner.pk})

        own_sale = WikiPropertySale.objects.create(location=self.wiki.location, created_by=self.viewer)
        stranger_sale = WikiPropertySale.objects.create(location=self.wiki.location, created_by=self.stranger)
        with _CONCEALED:
            visible_sales = visible_rows(WikiPropertySale.objects.for_location(self.wiki.location), self.wiki, self.viewer)
        self.assertEqual({s.pk for s in visible_sales}, {own_sale.pk})
        self.assertNotIn(stranger_sale.pk, {s.pk for s in visible_sales})

    def test_wiki_owner_official_record_with_no_creator_is_automatic_not_hidden(self) -> None:
        """plugins.builtin.property_records writes OwnerSource.OFFICIAL rows with no created_by.

        Null actor here is genuinely ambiguous (also caused by account
        deletion), so - like WikiLink - it defaults to automatic rather than
        being treated as a departed account. A concealed viewer must still
        see a real deed-lookup record; that's what a fresh wiki would show.
        """
        from urbanlens.dashboard.models.property_owner.meta import OwnerSource

        official = WikiOwner.objects.create(name="County Record LLC", source=OwnerSource.OFFICIAL, created_by=None)
        official.locations.add(self.wiki.location)
        with _CONCEALED:
            visible = visible_rows(WikiOwner.objects.for_location(self.wiki.location), self.wiki, self.viewer)
        self.assertIn(official.pk, {o.pk for o in visible})

    def test_reactions(self) -> None:
        from urbanlens.dashboard.models.comments.model import Comment
        from urbanlens.dashboard.models.reactions.model import Reaction

        comment = baker.make(Comment, wiki=self.wiki, pin=None, profile=self.viewer, text="mine")
        own_reaction = baker.make(Reaction, comment=comment, profile=self.viewer, emoji="👍")
        friend_reaction = baker.make(Reaction, comment=comment, profile=self.friend, emoji="👍")
        baker.make(Reaction, comment=comment, profile=self.stranger, emoji="👍")

        from urbanlens.dashboard.services.wiki.concealment import conceal_rows

        with _CONCEALED:
            visible = conceal_rows(comment.reactions.all(), self.viewer)
        self.assertEqual({r.pk for r in visible}, {own_reaction.pk, friend_reaction.pk})


class LayerUuidNullingAndWriteSafetyTests(TestCase):
    """The read-side layer_uuid nulling, and the write-path fix that stops it destroying real data.

    Round 4's adversarial review caught the write-safety bug directly: nulling
    a hidden layer for *display* means the edit panel's own <select> shows no
    selection, so editing any other field on the item echoes that None back
    as if clearing the layer had been deliberate - silently and permanently
    stripping a real, invisible layer assignment. test_editing_an_unrelated_field_
    does_not_clear_a_hidden_layer is that exact scenario end to end.
    """

    def setUp(self) -> None:
        super().setUp()
        self.wiki, self.viewer, self.friend, self.stranger = _wiki_with_viewer_friend_stranger()
        self.stranger_layer = baker.make(CustomLayer, parent_wiki=self.wiki, profile=self.stranger, name="STRANGER-LAYER")
        self.item = baker.make(
            PinMarkup,
            parent_wiki=self.wiki,
            profile=self.viewer,
            markup_type="line",
            geometry={"type": "LineString", "coordinates": [[0, 0], [1, 1]]},
            layer=self.stranger_layer,
            label="original",
        )
        self.client.force_login(self.viewer.user)

    def test_markup_json_nulls_the_layer_uuid_of_a_hidden_layer(self) -> None:
        """The item itself (own work) stays visible; its layer reference to an invisible layer does not."""
        with _CONCEALED:
            response = self.client.get(reverse("location.wiki.markup.json", args=[self.wiki.location.slug]))
        self.assertEqual(response.status_code, 200)
        entries = {e["uuid"]: e for e in response.json()["markup_items"]}
        self.assertIn(str(self.item.uuid), entries)
        self.assertIsNone(entries[str(self.item.uuid)]["layer_uuid"])

    def test_editing_an_unrelated_field_does_not_clear_a_hidden_layer(self) -> None:
        """The critical fix: a label edit must not round-trip the display-nulled layer_uuid into a real clear."""
        with _CONCEALED:
            response = self.client.post(
                reverse("location.wiki.markup.edit", args=[self.wiki.location.slug, self.item.uuid]),
                data=json.dumps({"label": "edited", "layer_uuid": None}),
                content_type="application/json",
            )
        self.assertEqual(response.status_code, 200)
        self.item.refresh_from_db()
        self.assertEqual(self.item.label, "edited")
        self.assertEqual(self.item.layer_id, self.stranger_layer.pk)

    def test_clearing_a_visible_layer_still_works(self) -> None:
        """The fix must not disable clearing outright - only when the real layer is invisible to this viewer."""
        own_layer = baker.make(CustomLayer, parent_wiki=self.wiki, profile=self.viewer, name="OWN-LAYER")
        self.item.layer = own_layer
        self.item.save(update_fields=["layer"])
        with _CONCEALED:
            response = self.client.post(
                reverse("location.wiki.markup.edit", args=[self.wiki.location.slug, self.item.uuid]),
                data=json.dumps({"layer_uuid": None}),
                content_type="application/json",
            )
        self.assertEqual(response.status_code, 200)
        self.item.refresh_from_db()
        self.assertIsNone(self.item.layer_id)

    def test_concealment_off_a_stray_empty_layer_uuid_still_clears_normally(self) -> None:
        """No behaviour change for the ordinary, non-concealed case."""
        response = self.client.post(
            reverse("location.wiki.markup.edit", args=[self.wiki.location.slug, self.item.uuid]),
            data=json.dumps({"layer_uuid": None}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        self.item.refresh_from_db()
        self.assertIsNone(self.item.layer_id)


class WikiOwnerDedupTests(TestCase):
    """The property_owner.py get_or_create fix: a concealed viewer must never merge into an invisible row."""

    def setUp(self) -> None:
        super().setUp()
        self.wiki, self.viewer, self.friend, self.stranger = _wiki_with_viewer_friend_stranger()
        self.client.force_login(self.viewer.user)

    def test_adding_an_owner_matching_an_invisible_strangers_name_creates_a_new_visible_row(self) -> None:
        stranger_owner = WikiOwner.objects.create(name="Alice", created_by=self.stranger)
        stranger_owner.locations.add(self.wiki.location)

        with _CONCEALED:
            response = self.client.post(
                reverse("location.wiki.ownership", args=[self.wiki.location.slug]),
                {"name": "Alice"},
            )
        self.assertEqual(response.status_code, 200)

        new_owners = WikiOwner.objects.for_location(self.wiki.location).filter(name="Alice").exclude(pk=stranger_owner.pk)
        self.assertEqual(new_owners.count(), 1)
        self.assertEqual(new_owners.first().created_by_id, self.viewer.pk)
        # The panel actually shows the submission worked, rather than a
        # silent no-op that only looks the same as success.
        self.assertIn(b"Alice", response.content)

    def test_sale_history_never_names_a_stranger_via_the_dedup_match(self) -> None:
        stranger_owner = WikiOwner.objects.create(name="Bob Stranger", created_by=self.stranger)
        stranger_owner.locations.add(self.wiki.location)

        with _CONCEALED:
            response = self.client.post(
                reverse("location.wiki.sales", args=[self.wiki.location.slug]),
                {"new_owners": "Bob Stranger"},
            )
        self.assertEqual(response.status_code, 200)

        sale = WikiPropertySale.objects.get(location=self.wiki.location)
        matched_owner = sale.new_owners.get()
        self.assertNotEqual(matched_owner.pk, stranger_owner.pk)
        self.assertEqual(matched_owner.created_by_id, self.viewer.pk)


class WikiScopedBoundaryConcealmentTests(TestCase):
    """A wiki-drawn boundary has no author at all - so it's hidden outright, except for the request that just drew it."""

    def setUp(self) -> None:
        super().setUp()
        self.wiki, self.viewer, self.friend, self.stranger = _wiki_with_viewer_friend_stranger()
        self.client.force_login(self.viewer.user)

    def test_get_falls_back_to_place_or_circle_when_concealed(self) -> None:
        from urbanlens.dashboard.models.boundary.model import Boundary, BoundaryType

        baker.make(
            Boundary,
            wiki=self.wiki,
            location=self.wiki.location,
            boundary_type=BoundaryType.PROPERTY,
            polygon="MULTIPOLYGON(((0 0, 0 1, 1 1, 1 0, 0 0)))",
        )
        with _CONCEALED:
            response = self.client.get(reverse("location.wiki.boundary", args=[self.wiki.location.slug]))
        self.assertEqual(response.status_code, 200)
        self.assertNotEqual(response.json()["boundaries"]["property"]["source"], "wiki")

    def test_post_shows_the_viewers_own_just_drawn_boundary_in_the_same_response(self) -> None:
        """The self-inconsistency the review caught: drawing your own boundary must not make it vanish."""
        polygon = {"type": "MultiPolygon", "coordinates": [[[[0, 0], [0, 0.0005], [0.0005, 0.0005], [0.0005, 0], [0, 0]]]]}
        with _CONCEALED:
            response = self.client.post(
                reverse("location.wiki.boundary", args=[self.wiki.location.slug]),
                data=json.dumps({"boundary_type": "property", "polygon": polygon}),
                content_type="application/json",
            )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["boundaries"]["property"]["source"], "wiki")
        self.assertIsNotNone(body["boundaries"]["property"]["polygon"])

    def test_a_later_get_still_hides_it_from_the_same_concealed_viewer(self) -> None:
        """The POST override is scoped to that one response - a subsequent GET has no such certainty and must hide it again."""
        polygon = {"type": "MultiPolygon", "coordinates": [[[[0, 0], [0, 0.0005], [0.0005, 0.0005], [0.0005, 0], [0, 0]]]]}
        with _CONCEALED:
            self.client.post(
                reverse("location.wiki.boundary", args=[self.wiki.location.slug]),
                data=json.dumps({"boundary_type": "property", "polygon": polygon}),
                content_type="application/json",
            )
            response = self.client.get(reverse("location.wiki.boundary", args=[self.wiki.location.slug]))
        self.assertNotEqual(response.json()["boundaries"]["property"]["source"], "wiki")


class SearchResultUsesConcealedValuesTests(TestCase):
    """A surviving search candidate's displayed text must be the concealed value, not the live row.

    This is the critical content-leak the fourth round's adversarial review
    caught in the search fix itself: the over-fetch+reverify gate only
    decided whether a concealed wiki could appear in results at all: the
    SearchResult it then built still read wiki.name/description straight off
    the live row. A term matching only via a friend's alias must not display
    the wiki's true, stranger-renamed title.
    """

    def setUp(self) -> None:
        super().setUp()
        self.wiki, self.viewer, self.friend, self.stranger = _wiki_with_viewer_friend_stranger()
        self.wiki.location.slug = "old-mill"
        self.wiki.location.save(update_fields=["slug"])

    def test_result_title_and_snippet_are_the_concealed_values(self) -> None:
        from urbanlens.dashboard.models.abstract.versioning import WriteSource, writing_as
        from urbanlens.dashboard.models.aliases.model import WikiAlias
        from urbanlens.dashboard.services.global_search.parser import parse_query
        from urbanlens.dashboard.services.global_search.providers import WikiSearchProvider

        with writing_as(WriteSource.USER, actor=self.stranger.pk):
            Wiki.objects.filter(pk=self.wiki.pk).update(name="Haunted Steel Mill", description="stranger's security notes")
        baker.make(WikiAlias, wiki=self.wiki, name="Old Warehouse", created_by=self.friend)

        parsed = parse_query("warehouse")
        with _CONCEALED:
            results = WikiSearchProvider().search(self.viewer, parsed, limit=10)

        self.assertEqual(len(results), 1)
        self.assertNotIn("Haunted Steel Mill", results[0].title)
        self.assertNotIn("stranger's security notes", results[0].snippet or "")


class AutocompleteUsesConcealedValuesTests(TestCase):
    """search_local's map-search-bar wiki result has the identical fix, and identical risk of drift."""

    def setUp(self) -> None:
        super().setUp()
        self.wiki, self.viewer, self.friend, self.stranger = _wiki_with_viewer_friend_stranger()
        self.wiki.location.slug = "old-mill"
        self.wiki.location.save(update_fields=["slug"])

    def test_wiki_result_title_is_the_concealed_value(self) -> None:
        from urbanlens.dashboard.models.abstract.versioning import WriteSource, writing_as
        from urbanlens.dashboard.services.map_pins.autocomplete import search_local

        with writing_as(WriteSource.USER, actor=self.stranger.pk):
            Wiki.objects.filter(pk=self.wiki.pk).update(name="Haunted Steel Mill")

        with _CONCEALED:
            results = search_local("haunted", self.viewer)

        # The live name doesn't survive concealment for this viewer, so the
        # substring match against it must not produce a "location" result -
        # and if the gate were somehow bypassed, its title must never be the
        # live name either.
        wiki_results = [r for r in results if r.type == "location"]
        self.assertTrue(all("Haunted Steel Mill" not in (r.title or "") for r in wiki_results))

    def test_pin_subtitle_wiki_alias_fallback_only_uses_visible_aliases(self) -> None:
        from urbanlens.dashboard.models.aliases.model import WikiAlias
        from urbanlens.dashboard.models.pin.model import Pin
        from urbanlens.dashboard.services.map_pins.autocomplete import search_local

        # _wiki_with_viewer_friend_stranger already pinned the viewer here -
        # a second Pin for the same (location, profile) violates the unique
        # constraint, so reuse it and just link it to the wiki.
        pin = Pin.objects.get(profile=self.viewer, location=self.wiki.location)
        pin.wiki = self.wiki
        pin.name = ""
        pin.save(update_fields=["wiki", "name"])
        baker.make(WikiAlias, wiki=self.wiki, name="Stranger Depot Yard", created_by=self.stranger)

        with _CONCEALED:
            results = search_local("depot", self.viewer)

        pin_results = [r for r in results if r.pin_slug == pin.slug]
        self.assertTrue(all("Stranger Depot Yard" not in r.subtitle for r in pin_results))
