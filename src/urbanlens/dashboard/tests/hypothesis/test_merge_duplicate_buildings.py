"""Duplicate building places - one real building provisioned twice under drifting Overture refs - merge into one (REData P98)."""

from __future__ import annotations

from io import StringIO
import re
from typing import Any
from unittest import mock

from django.contrib.auth.models import User
from django.core.management import call_command
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.article.model import Article, ArticleRevision
from urbanlens.dashboard.models.boundary.model import Boundary, BoundaryRevision
from urbanlens.dashboard.models.facts.model import Fact
from urbanlens.dashboard.models.floorplans.model import Floorplan
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin, PinType
from urbanlens.dashboard.models.pin_merge_suggestions.model import PinMergeSuggestion, PinMergeSuggestionOrigin
from urbanlens.dashboard.models.place.merge import PlaceMerge
from urbanlens.dashboard.models.place.model import Place, PlaceKind, PlaceStatus
from urbanlens.dashboard.models.wiki.model import Wiki
from urbanlens.dashboard.models.wiki_edit.model import WikiEdit
from urbanlens.dashboard.models.wiki_stat_vote.model import WikiStatVote
from urbanlens.dashboard.services.places import building_merge, lineage, overture_refs
from urbanlens.dashboard.services.wiki.articles import save_article
from urbanlens.dashboard.services.wiki.duplicate_wikis import WIKI_MERGED_CHANGE

from .place_helpers import make_place
from .test_overture_stable_refs import FakeResolver, _record, _resolved, _square

_STABLE = "overture:08b2a100d2c8dfff0200f8b4d3e8a1c7"
_LEGACY = "overture:aaaaaaaaaaaa"
_OTHER_LEGACY = "overture:cccccccccccc"


class _DuplicatesTestCase(TestCase):
    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # absorbs the bootstrap site-admin promotion
        self.parcel = self._redata_parcel(_square(0.0, 0.0, 1.0), "11111111-1111-1111-1111-111111111111")
        self.survivor = self._building(_STABLE, _square(0.2, 0.2))
        self.loser = self._building(_LEGACY, _square(0.201, 0.2))
        self.resolver = FakeResolver({_LEGACY: _resolved(_STABLE)})

    def _redata_parcel(self, shape, key: str) -> Place:
        parcel = make_place(PlaceKind.PARCEL, shape, name=key)
        Place.objects.filter(pk=parcel.pk).update(provider="redata", provider_key=key)
        parcel.refresh_from_db()
        return parcel

    def _building(self, key: str, shape, parent: Place | None = None) -> Place:
        place = make_place(PlaceKind.BUILDING, shape, parent=parent or self.parcel, name=key)
        Place.objects.filter(pk=place.pk).update(provider="redata", provider_key=key)
        place.refresh_from_db()
        return place

    def _location(self, place: Place, offset: float = 0.0) -> Location:
        centroid = place.geometry.centroid
        location = Location.objects.create(
            latitude=round(centroid.y + offset, 6), longitude=round(centroid.x + offset, 6)
        )
        Location.objects.filter(pk=location.pk).update(place=place)
        location.refresh_from_db()
        return location

    def _wiki(self, place: Place, name: str) -> Wiki:
        return baker.make(
            Wiki, location=self._location(place, 0.001), place=place, name=name, pin_type=PinType.BUILDING
        )

    def detect(self, **kwargs: Any) -> building_merge.Detection:
        return building_merge.detect_duplicate_buildings(resolver=self.resolver, **kwargs)

    def merge(self) -> list[PlaceMerge]:
        detection = self.detect()
        return [record for group in detection.groups for record in building_merge.merge_group(group)]


class DetectionTests(_DuplicatesTestCase):
    def test_a_legacy_place_resolving_to_a_held_stable_ref_is_a_group(self) -> None:
        detection = self.detect()

        self.assertEqual(len(detection.groups), 1)
        group = detection.groups[0]
        self.assertEqual(group.survivor.pk, self.survivor.pk)
        self.assertEqual([loser.pk for loser in group.losers], [self.loser.pk])
        self.assertEqual(group.gers_id, _STABLE.removeprefix("overture:"))

    def test_a_hash_redata_still_serves_by_that_exact_ref_is_a_group(self) -> None:
        """REData serving the old hash with its stable ref beside it is the same identity record as a resolve."""
        self.resolver = FakeResolver()
        detection = self.detect(fetch_buildings=lambda _uuid: [_record(_LEGACY, _square(0.5, 0.5), stable_ref=_STABLE)])

        self.assertEqual([group.survivor.pk for group in detection.groups], [self.survivor.pk])

    def test_a_footprint_only_match_is_reported_not_merged(self) -> None:
        self.resolver = FakeResolver()
        detection = self.detect(
            fetch_buildings=lambda _uuid: [_record("overture:dddddddddddd", _square(0.2005, 0.2), stable_ref=_STABLE)]
        )

        self.assertEqual(detection.groups, [])
        self.assertIn(building_merge.FOOTPRINT_ONLY, [finding.kind for finding in detection.findings])

    def test_members_under_different_parcels_are_reported_not_merged(self) -> None:
        other_parcel = self._redata_parcel(_square(2.0, 2.0, 1.0), "22222222-2222-2222-2222-222222222222")
        Place.objects.filter(pk=self.loser.pk).delete()
        self._building(_LEGACY, _square(2.2, 2.2), parent=other_parcel)

        detection = self.detect()

        self.assertEqual(detection.groups, [])
        self.assertEqual(
            [finding.kind for finding in detection.findings if finding.kind != building_merge.CROSS_SOURCE_FOOTPRINT],
            [building_merge.DIFFERENT_PARCELS],
        )

    def test_two_sources_holding_each_others_centroids_are_reported_not_merged(self) -> None:
        osm = make_place(PlaceKind.BUILDING, _square(0.2, 0.2), parent=self.parcel, name="osm")
        Place.objects.filter(pk=osm.pk).update(provider="redata", provider_key="osm:way/42")

        detection = self.detect()

        cross = [finding for finding in detection.findings if finding.kind == building_merge.CROSS_SOURCE_FOOTPRINT]
        self.assertTrue(any(osm.pk in finding.places and self.survivor.pk in finding.places for finding in cross))
        self.assertEqual(len(detection.groups), 1, "the OSM place must not join the stable-ref group")
        self.assertNotIn(osm.pk, [member.pk for member in detection.groups[0].members])

    def test_a_placeless_building_wiki_inside_a_wikid_building_is_reported(self) -> None:
        own = self._wiki(self.survivor, "Old Mill")
        placeless = baker.make(
            Wiki,
            location=self._location(self.survivor, 0.002),
            place=None,
            name="Mill (campus copy)",
            pin_type=PinType.BUILDING,
        )
        Wiki.objects.filter(pk=placeless.pk).update(place=None)

        detection = self.detect()

        placeless_findings = [
            finding for finding in detection.findings if finding.kind == building_merge.PLACELESS_WIKI
        ]
        self.assertEqual([finding.wikis for finding in placeless_findings], [(placeless.pk, own.pk)])

    def test_with_no_holder_the_place_with_a_wiki_survives(self) -> None:
        Place.objects.filter(pk=self.survivor.pk).update(provider_key=_OTHER_LEGACY)
        self.resolver = FakeResolver({_LEGACY: _resolved(_STABLE), _OTHER_LEGACY: _resolved(_STABLE)})
        self._wiki(self.loser, "Grist Works")

        detection = self.detect()

        self.assertEqual(detection.groups[0].survivor.pk, self.loser.pk)


class MergeTests(_DuplicatesTestCase):
    def test_the_losers_locations_move_and_it_is_superseded_not_deleted(self) -> None:
        location = self._location(self.loser)

        [record] = self.merge()

        location.refresh_from_db()
        self.assertEqual(location.place_id, self.survivor.pk)
        self.loser.refresh_from_db()
        self.assertEqual(self.loser.status, PlaceStatus.SUPERSEDED)
        self.assertEqual(self.loser.provider_key, "")
        self.assertEqual(
            (record.survivor_id, record.loser_id, record.old_keys),
            (self.survivor.pk, self.loser.pk, [f"redata:{_LEGACY}"]),
        )
        self.assertEqual(record.moved["locations"], [location.pk])

    def test_a_second_run_does_nothing(self) -> None:
        self._location(self.loser)
        self.merge()

        self.assertEqual(self.merge(), [])
        self.assertEqual(PlaceMerge.objects.count(), 1)
        self.assertEqual(self.detect().groups, [])

    def test_with_no_holder_the_survivor_takes_the_stable_ref(self) -> None:
        Place.objects.filter(pk=self.survivor.pk).update(provider_key=_OTHER_LEGACY)
        self.resolver = FakeResolver({_LEGACY: _resolved(_STABLE), _OTHER_LEGACY: _resolved(_STABLE)})

        self.merge()

        survivor = Place.objects.get(pk__in=[self.survivor.pk, self.loser.pk], status=PlaceStatus.CURRENT)
        self.assertEqual(survivor.provider_key, _STABLE)

    def test_floorplans_and_swept_buildings_naming_the_loser_follow_it(self) -> None:
        profile = baker.make(User).profile
        floorplan = baker.make(Floorplan, profile=profile, building_ref=_LEGACY, place=self.loser)
        pin = baker.make_recipe("dashboard.pin", profile=profile)
        Pin.objects.filter(pk=pin.pk).update(auto_nested_buildings=[{"ref": _LEGACY, "lat": 0.25, "lng": 0.25}])

        self.merge()

        floorplan.refresh_from_db()
        self.assertEqual((floorplan.building_ref, floorplan.place_id), (_STABLE, self.survivor.pk))
        pin.refresh_from_db()
        self.assertEqual(pin.auto_nested_buildings[0]["ref"], _STABLE)

    def test_the_parcel_stops_counting_the_superseded_building(self) -> None:
        self.parcel.refresh_from_db()
        self.assertEqual(self.parcel.building_child_count, 2)

        self.merge()

        self.parcel.refresh_from_db()
        self.assertEqual(self.parcel.building_child_count, 1)

    def test_the_floorplan_editor_stops_offering_the_superseded_building(self) -> None:
        """A one-building parcel after a merge resolves to that building, not a picker listing the loser too."""
        from urbanlens.dashboard.controllers.floorplans import _building_choices, _building_place

        location = Location.objects.create(latitude=0.9, longitude=0.9)
        Location.objects.filter(pk=location.pk).update(place=self.parcel)
        pin = baker.make_recipe(
            "dashboard.pin", profile=baker.make(User).profile, location=location, pin_type=PinType.PARCEL
        )

        self.merge()

        pin = Pin.objects.select_related("location__place").get(pk=pin.pk)
        self.assertEqual(_building_place(pin), self.survivor)
        self.assertEqual(_building_choices(pin), [])

    def test_building_child_count_counts_only_current_buildings(self) -> None:
        Place.objects.filter(pk=self.loser.pk).update(status=PlaceStatus.SUPERSEDED)

        lineage.refresh_derived_flags([self.parcel.pk])

        self.parcel.refresh_from_db()
        self.assertEqual(self.parcel.building_child_count, 1)

    def test_every_relation_to_place_is_handled(self) -> None:
        """A relation to Place the merge never mentions stays on the superseded loser, away from everything that moved."""
        import inspect

        source = inspect.getsource(building_merge)
        unhandled = sorted(
            f"{rel.related_model.__name__}.{rel.field.name}"
            for rel in Place._meta.related_objects
            if rel.related_model is not PlaceMerge
            and not re.search(
                rf"\b{rel.related_model.__name__}\.objects\.filter\({rel.field.name}=|\b{rel.related_model.__name__}\.objects\.filter\({rel.field.name}_id",
                source,
            )
        )
        self.assertEqual(unhandled, ["Place.domain_root"], "only the domain root is left to lineage.set_parent")


class PinSuggestionTests(_DuplicatesTestCase):
    def test_an_owner_with_a_pin_on_each_side_gets_a_suggestion_not_a_merge(self) -> None:
        profile = baker.make(User).profile
        kept = baker.make_recipe(
            "dashboard.pin", profile=profile, location=self._location(self.survivor), pin_type=PinType.BUILDING
        )
        other = baker.make_recipe(
            "dashboard.pin", profile=profile, location=self._location(self.loser), pin_type=PinType.BUILDING
        )

        self.merge()

        self.assertTrue(
            Pin.objects.filter(pk__in=[kept.pk, other.pk]).count() == 2, "pins are never merged automatically"
        )
        suggestion = PinMergeSuggestion.objects.get(profile=profile)
        self.assertEqual({suggestion.pin_a_id, suggestion.pin_b_id}, {kept.pk, other.pk})
        self.assertEqual(suggestion.origin, PinMergeSuggestionOrigin.DUPLICATE_BUILDING)

    def test_an_entrance_pin_is_not_a_duplicate_of_the_building(self) -> None:
        profile = baker.make(User).profile
        baker.make_recipe(
            "dashboard.pin", profile=profile, location=self._location(self.survivor), pin_type=PinType.BUILDING
        )
        baker.make_recipe(
            "dashboard.pin", profile=profile, location=self._location(self.loser), pin_type=PinType.ENTRANCE
        )

        self.merge()

        self.assertFalse(PinMergeSuggestion.objects.exists())


class WikiMergeTests(_DuplicatesTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.alice = baker.make(User).profile
        self.bob = baker.make(User).profile
        self.kept = self._wiki(self.survivor, "Old Mill")
        self.absorbed = self._wiki(self.loser, "Grist Works")

    def test_a_loser_wiki_moves_when_the_survivor_has_none(self) -> None:
        Wiki.objects.filter(pk=self.kept.pk).delete()

        self.merge()

        self.absorbed.refresh_from_db()
        self.assertEqual(self.absorbed.place_id, self.survivor.pk)

    def test_the_article_keeps_survivor_text_live_and_appends_the_losers_as_a_section(self) -> None:
        save_article(editor=self.alice, content="Built 1890.", wiki=self.kept)
        save_article(editor=self.bob, content="Draft", wiki=self.absorbed)
        save_article(editor=self.bob, content="Milled grain until 1950.", wiki=self.absorbed)

        self.merge()

        article = Article.objects.get(wiki=self.kept)
        self.assertTrue(article.content.startswith("Built 1890."))
        self.assertIn("## Merged from Grist Works\n\nMilled grain until 1950.", article.content)
        history = list(article.revisions.order_by("-created", "-pk"))
        self.assertEqual(history[0].content, article.content)
        self.assertEqual(history[0].editor_id, self.bob.pk, "attributed to the loser article's last human editor")
        self.assertEqual(history[0].edit_summary, "Merged from Grist Works")
        self.assertTrue({"Draft", "Milled grain until 1950."} <= {revision.content for revision in history})
        self.assertEqual(Article.objects.filter(wiki__isnull=False).count(), 1)

    def test_a_concealed_editors_merge_revision_carries_only_what_they_could_see(self) -> None:
        save_article(editor=self.alice, content="Alice's private history.", wiki=self.kept)
        save_article(editor=self.bob, content="Bob's notes.", wiki=self.absorbed)

        with mock.patch("urbanlens.dashboard.services.wiki.concealment.concealment_active", return_value=True):
            self.merge()

        article = Article.objects.get(wiki=self.kept)
        self.assertIn("Alice's private history.", article.content)
        merged = ArticleRevision.objects.get(article=article, edit_summary="Merged from Grist Works")
        self.assertNotIn("Alice's private history.", merged.content)
        self.assertIn("Bob's notes.", merged.content)

    def test_text_last_written_by_a_deleted_account_is_credited_to_nobody(self) -> None:
        """Crediting the last *surviving* editor would attribute the deleted account's text to someone who never wrote it."""
        save_article(editor=self.alice, content="Built 1890.", wiki=self.kept)
        save_article(editor=self.bob, content="Bob's draft.", wiki=self.absorbed)
        save_article(editor=None, content="Rewritten by an account since deleted.", wiki=self.absorbed)

        self.merge()

        merged = ArticleRevision.objects.get(article__wiki=self.kept, edit_summary="Merged from Grist Works")
        self.assertIsNone(merged.editor_id)

    def test_identical_text_is_not_appended(self) -> None:
        save_article(editor=self.alice, content="Built 1890.", wiki=self.kept)
        save_article(editor=self.bob, content="Built 1890.", wiki=self.absorbed)

        self.merge()

        self.assertEqual(Article.objects.get(wiki=self.kept).content, "Built 1890.")

    def test_the_loser_wiki_is_deleted_and_the_merge_is_a_system_edit(self) -> None:
        comment = baker.make_recipe("dashboard.comment", wiki=self.absorbed, pin=None, profile=self.alice)

        self.merge()

        self.assertFalse(Wiki.objects.filter(pk=self.absorbed.pk).exists())
        comment.refresh_from_db()
        self.assertEqual(comment.wiki_id, self.kept.pk)
        edit = WikiEdit.objects.get(wiki=self.kept, editor=None)
        self.assertEqual(edit.changes[WIKI_MERGED_CHANGE], {"from": "Grist Works", "to": "Old Mill"})

    def test_an_absorbed_wiki_further_up_the_tree_does_not_close_a_loop(self) -> None:
        """Kept -> Mid -> Absorbed: handing Mid to the kept page would make Kept -> Mid -> Kept."""
        mid = baker.make(
            Wiki, location=self._location(self.survivor, 0.003), place=None, name="Mid", pin_type=PinType.BUILDING
        )
        sibling = baker.make(
            Wiki, location=self._location(self.survivor, 0.004), place=None, name="Sibling", pin_type=PinType.BUILDING
        )
        Wiki.objects.filter(pk__in=[mid.pk, sibling.pk]).update(parent_wiki=self.absorbed)
        Wiki.objects.filter(pk=self.kept.pk).update(parent_wiki=mid)

        self.merge()

        self.kept.refresh_from_db()
        mid.refresh_from_db()
        sibling.refresh_from_db()
        self.assertIsNone(self.kept.parent_wiki_id, "the kept page takes the absorbed page's place in the tree")
        self.assertEqual((mid.parent_wiki_id, sibling.parent_wiki_id), (self.kept.pk, self.kept.pk))
        for wiki in (self.kept, mid, sibling):
            self.assertNotIn(wiki.pk, [ancestor.pk for ancestor in wiki.ancestor_chain()])

    def test_the_absorbed_wiki_as_direct_parent_is_still_unnested(self) -> None:
        Wiki.objects.filter(pk=self.kept.pk).update(parent_wiki=self.absorbed)

        self.merge()

        self.kept.refresh_from_db()
        self.assertIsNone(self.kept.parent_wiki_id)
        self.assertTrue(Wiki.objects.filter(pk=self.kept.pk).exists())

    def test_repointing_never_moves_an_ancestor_of_the_kept_row(self) -> None:
        """The general guard: an ancestor left in place is reported, not turned into a loop."""
        from urbanlens.dashboard.services.core.row_merge import remaining_references, repoint_references

        mid = baker.make(
            Wiki, location=self._location(self.survivor, 0.003), place=None, name="Mid", pin_type=PinType.BUILDING
        )
        Wiki.objects.filter(pk=mid.pk).update(parent_wiki=self.absorbed)
        Wiki.objects.filter(pk=self.kept.pk).update(parent_wiki=mid)

        repoint_references(self.absorbed, self.kept)

        mid.refresh_from_db()
        self.assertEqual(mid.parent_wiki_id, self.absorbed.pk)
        self.assertEqual([row.pk for row in remaining_references(self.absorbed)["Wiki.parent_wiki"]], [mid.pk])

    def test_per_key_conflicts_keep_the_survivors_and_record_the_losers(self) -> None:
        Fact.objects.create(wiki=self.kept, key="wiki_indoor_outdoor", value_text="indoor")
        Fact.objects.create(wiki=self.absorbed, key="wiki_indoor_outdoor", value_text="outdoor")
        moved_fact = Fact.objects.create(wiki=self.absorbed, key="wiki_pin_type", value_text="building")
        WikiStatVote.objects.create(wiki=self.kept, profile=self.alice, field="danger", value=2)
        WikiStatVote.objects.create(wiki=self.absorbed, profile=self.alice, field="danger", value=5)
        bobs = WikiStatVote.objects.create(wiki=self.absorbed, profile=self.bob, field="danger", value=4)

        [record] = self.merge()

        self.assertEqual(Fact.objects.get(wiki=self.kept, key="wiki_indoor_outdoor").value_text, "indoor")
        moved_fact.refresh_from_db()
        self.assertEqual(moved_fact.wiki_id, self.kept.pk)
        self.assertEqual(WikiStatVote.objects.get(wiki=self.kept, profile=self.alice).value, 2)
        bobs.refresh_from_db()
        self.assertEqual(bobs.wiki_id, self.kept.pk)
        dropped = record.dropped["wiki"]
        self.assertEqual([fact["fields"]["value_text"] for fact in dropped["facts"]], ["outdoor"])
        self.assertEqual([vote["fields"]["value"] for vote in dropped["stat_votes"]], [5])
        self.assertEqual(record.gers_id, _STABLE.removeprefix("overture:"))

    def test_a_losing_community_boundary_becomes_a_revision_the_merge_edit_names(self) -> None:
        Boundary.objects.create(
            wiki=self.kept, location=self.kept.location, boundary_type="building", polygon=_square(0.2, 0.2)
        )
        Boundary.objects.create(
            wiki=self.absorbed, location=self.absorbed.location, boundary_type="building", polygon=_square(0.3, 0.3)
        )

        self.merge()

        self.assertEqual(
            Boundary.objects.get(wiki=self.kept, boundary_type="building").polygon.coords, _square(0.2, 0.2).coords
        )
        change = WikiEdit.objects.get(wiki=self.kept, editor=None).changes["boundary_building"]
        lost = BoundaryRevision.objects.get(pk=change["from"])
        self.assertEqual((lost.wiki_id, lost.polygon.coords), (self.kept.pk, _square(0.3, 0.3).coords))

    def test_differing_columns_keep_the_survivors_value_and_fill_its_blanks(self) -> None:
        Wiki.objects.filter(pk=self.kept.pk).update(description="", date_abandoned=None)
        Wiki.objects.filter(pk=self.absorbed.pk).update(description="Grist mill.", fences="some")

        [record] = self.merge()

        self.kept.refresh_from_db()
        self.assertEqual((self.kept.name, self.kept.description, self.kept.fences), ("Old Mill", "Grist mill.", "some"))
        self.assertEqual(record.dropped["wiki"]["columns"]["name"], "Grist Works")
        self.assertTrue(self.kept.aliases.filter(name__iexact="Grist Works").exists())


class CommandTests(_DuplicatesTestCase):
    def _run(self, *args: str) -> str:
        out = StringIO()
        with mock.patch.object(overture_refs, "RedataResolver", return_value=self.resolver):
            call_command("merge_duplicate_buildings", *args, stdout=out, stderr=StringIO())
        return out.getvalue()

    def test_a_dry_run_reports_and_writes_nothing(self) -> None:
        self._location(self.loser)

        output = self._run()

        self.assertIn(f"survivor place {self.survivor.pk} {_STABLE}", output)
        self.assertIn(f"loser    place {self.loser.pk} {_LEGACY}: locations: 1", output)
        self.assertIn("Nothing was written", output)
        self.loser.refresh_from_db()
        self.assertEqual(self.loser.status, PlaceStatus.CURRENT)
        self.assertFalse(PlaceMerge.objects.exists())

    def test_apply_merges_and_a_second_apply_finds_nothing(self) -> None:
        self.assertIn(f"merged place {self.loser.pk} into {self.survivor.pk}", self._run("--apply"))

        self.assertIn("0 duplicate group(s)", self._run("--apply"))
        self.assertEqual(PlaceMerge.objects.count(), 1)
