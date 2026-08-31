"""Tests for services.locations.external_tag_groups - equivalence-group resolution and mutations."""

from __future__ import annotations

from model_bakery import baker

from urbanlens.core.tests.testcase import SimpleTestCase, TestCase
from urbanlens.dashboard.models.place.external_tag import ExternalTagSource, ExtractedTag, PlaceExternalTag
from urbanlens.dashboard.models.place.external_tag_group import ExternalTagGroup, ExternalTagVocabularyEntry
from urbanlens.dashboard.models.place.model import Place
from urbanlens.dashboard.services.locations.external_tag_groups import (
    ExternalTagGroupError,
    create_group,
    default_group_key,
    move_entry,
    set_preferred,
    suggested_clusters,
    visible_tags_for_place,
)


class DefaultGroupKeyTests(SimpleTestCase):
    def test_matches_humanized_case_and_whitespace_insensitively(self):
        self.assertEqual(default_group_key("Restaurant"), default_group_key("restaurant"))
        self.assertEqual(default_group_key(" restaurant "), default_group_key("restaurant"))

    def test_underscored_values_normalize_the_same_as_their_spaced_form(self):
        self.assertEqual(default_group_key("single_family_residential"), default_group_key("single family residential"))


def _sync(place: Place, source: str, key: str, value: str, *, is_primary: bool = False) -> None:
    PlaceExternalTag.sync_for_source(place, source, [ExtractedTag(key=key, value=value, is_primary=is_primary)])


class VisibleTagsForPlaceTests(TestCase):
    def test_a_lone_tag_with_no_synonym_passes_through_unchanged(self):
        place = baker.make(Place)
        _sync(place, ExternalTagSource.OSM, "amenity", "restaurant")

        visible = visible_tags_for_place(place)

        self.assertEqual([(t.source, t.key, t.value) for t in visible], [(ExternalTagSource.OSM, "amenity", "restaurant")])

    def test_two_tags_with_the_same_display_text_collapse_by_default(self):
        place = baker.make(Place)
        _sync(place, ExternalTagSource.OSM, "amenity", "restaurant")
        # Overture sync would collide with OSM's row on (place, source, key,
        # value) uniqueness only if identical on all four - different source
        # keeps this a separate row, which is the point being tested.
        PlaceExternalTag.objects.bulk_create([PlaceExternalTag(place=place, source=ExternalTagSource.OVERTURE, key="building_subtype", value="restaurant")])
        ExternalTagVocabularyEntry.objects.get_or_create(source=ExternalTagSource.OVERTURE, key="building_subtype", value="restaurant")

        visible = visible_tags_for_place(place)

        self.assertEqual(len(visible), 1)

    def test_two_tags_in_different_explicit_groups_do_not_collapse_even_with_matching_text(self):
        place = baker.make(Place)
        _sync(place, ExternalTagSource.OSM, "amenity", "restaurant")
        PlaceExternalTag.objects.bulk_create([PlaceExternalTag(place=place, source=ExternalTagSource.OVERTURE, key="building_subtype", value="restaurant")])
        osm_entry, _ = ExternalTagVocabularyEntry.objects.get_or_create(source=ExternalTagSource.OSM, key="amenity", value="restaurant")
        overture_entry, _ = ExternalTagVocabularyEntry.objects.get_or_create(source=ExternalTagSource.OVERTURE, key="building_subtype", value="restaurant")
        create_group([osm_entry.pk])
        create_group([overture_entry.pk])

        visible = visible_tags_for_place(place)

        self.assertEqual(len(visible), 2)

    def test_explicit_groups_preferred_member_wins_when_present_on_this_place(self):
        place = baker.make(Place)
        _sync(place, ExternalTagSource.OSM, "amenity", "restaurant")
        PlaceExternalTag.objects.bulk_create([PlaceExternalTag(place=place, source=ExternalTagSource.OVERTURE, key="building_subtype", value="restaurant")])
        osm_entry, _ = ExternalTagVocabularyEntry.objects.get_or_create(source=ExternalTagSource.OSM, key="amenity", value="restaurant")
        overture_entry, _ = ExternalTagVocabularyEntry.objects.get_or_create(source=ExternalTagSource.OVERTURE, key="building_subtype", value="restaurant")
        create_group([osm_entry.pk, overture_entry.pk], preferred_id=overture_entry.pk)

        visible = visible_tags_for_place(place)

        self.assertEqual(len(visible), 1)
        self.assertEqual(visible[0].source, ExternalTagSource.OVERTURE)

    def test_falls_back_to_ordering_when_the_preferred_member_is_not_on_this_place(self):
        place = baker.make(Place)
        _sync(place, ExternalTagSource.OSM, "amenity", "restaurant", is_primary=True)
        osm_entry, _ = ExternalTagVocabularyEntry.objects.get_or_create(source=ExternalTagSource.OSM, key="amenity", value="restaurant")
        # The preferred member (overture) is never synced onto this place.
        overture_entry, _ = ExternalTagVocabularyEntry.objects.get_or_create(source=ExternalTagSource.OVERTURE, key="building_subtype", value="restaurant")
        create_group([osm_entry.pk, overture_entry.pk], preferred_id=overture_entry.pk)

        visible = visible_tags_for_place(place)

        self.assertEqual(len(visible), 1)
        self.assertEqual(visible[0].source, ExternalTagSource.OSM)

    def test_empty_place_returns_empty_list(self):
        place = baker.make(Place)

        self.assertEqual(visible_tags_for_place(place), [])


class SuggestedClustersTests(TestCase):
    def test_two_ungrouped_entries_with_matching_text_form_a_cluster(self):
        ExternalTagVocabularyEntry.objects.create(source=ExternalTagSource.OSM, key="amenity", value="restaurant")
        ExternalTagVocabularyEntry.objects.create(source=ExternalTagSource.OVERTURE, key="building_subtype", value="restaurant")

        clusters = suggested_clusters()

        self.assertEqual(len(clusters), 1)
        self.assertEqual(len(clusters[0].entries), 2)

    def test_a_grouped_entry_is_excluded_even_if_its_text_would_otherwise_match(self):
        a = ExternalTagVocabularyEntry.objects.create(source=ExternalTagSource.OSM, key="amenity", value="restaurant")
        ExternalTagVocabularyEntry.objects.create(source=ExternalTagSource.OVERTURE, key="building_subtype", value="restaurant")
        create_group([a.pk])

        clusters = suggested_clusters()

        self.assertEqual(clusters, [])

    def test_a_singleton_with_no_match_produces_no_cluster(self):
        ExternalTagVocabularyEntry.objects.create(source=ExternalTagSource.OSM, key="amenity", value="restaurant")

        self.assertEqual(suggested_clusters(), [])


class CreateGroupTests(TestCase):
    def test_creates_a_group_with_the_first_entry_preferred_by_default(self):
        a = ExternalTagVocabularyEntry.objects.create(source=ExternalTagSource.OSM, key="amenity", value="restaurant")
        b = ExternalTagVocabularyEntry.objects.create(source=ExternalTagSource.OVERTURE, key="building_subtype", value="restaurant")

        group = create_group([a.pk, b.pk])

        a.refresh_from_db()
        b.refresh_from_db()
        self.assertEqual(a.group_id, group.pk)
        self.assertEqual(b.group_id, group.pk)
        self.assertTrue(a.is_preferred)
        self.assertFalse(b.is_preferred)

    def test_honors_an_explicit_preferred_id(self):
        a = ExternalTagVocabularyEntry.objects.create(source=ExternalTagSource.OSM, key="amenity", value="restaurant")
        b = ExternalTagVocabularyEntry.objects.create(source=ExternalTagSource.OVERTURE, key="building_subtype", value="restaurant")

        create_group([a.pk, b.pk], preferred_id=b.pk)

        a.refresh_from_db()
        b.refresh_from_db()
        self.assertFalse(a.is_preferred)
        self.assertTrue(b.is_preferred)

    def test_a_single_entry_creates_a_singleton_group(self):
        a = ExternalTagVocabularyEntry.objects.create(source=ExternalTagSource.OSM, key="amenity", value="restaurant")

        group = create_group([a.pk])

        a.refresh_from_db()
        self.assertEqual(a.group_id, group.pk)

    def test_empty_selection_is_refused(self):
        with self.assertRaises(ExternalTagGroupError):
            create_group([])

    def test_an_already_grouped_entry_is_refused(self):
        a = ExternalTagVocabularyEntry.objects.create(source=ExternalTagSource.OSM, key="amenity", value="restaurant")
        b = ExternalTagVocabularyEntry.objects.create(source=ExternalTagSource.OVERTURE, key="building_subtype", value="restaurant")
        create_group([a.pk])

        with self.assertRaises(ExternalTagGroupError):
            create_group([a.pk, b.pk])

    def test_an_unknown_id_is_refused(self):
        a = ExternalTagVocabularyEntry.objects.create(source=ExternalTagSource.OSM, key="amenity", value="restaurant")

        with self.assertRaises(ExternalTagGroupError):
            create_group([a.pk, 999_999])


class MoveEntryTests(TestCase):
    def test_moving_an_ungrouped_entry_into_a_group_joins_it_as_non_preferred(self):
        preferred = ExternalTagVocabularyEntry.objects.create(source=ExternalTagSource.OSM, key="amenity", value="restaurant")
        group = create_group([preferred.pk])
        joiner = ExternalTagVocabularyEntry.objects.create(source=ExternalTagSource.OVERTURE, key="building_subtype", value="restaurant")

        result = move_entry(joiner.pk, group.pk)

        joiner.refresh_from_db()
        self.assertIsNone(result)
        self.assertEqual(joiner.group_id, group.pk)
        self.assertFalse(joiner.is_preferred)

    def test_moving_the_only_other_member_out_deletes_the_now_empty_group(self):
        a = ExternalTagVocabularyEntry.objects.create(source=ExternalTagSource.OSM, key="amenity", value="restaurant")
        b = ExternalTagVocabularyEntry.objects.create(source=ExternalTagSource.OVERTURE, key="building_subtype", value="restaurant")
        group = create_group([a.pk, b.pk])

        result = move_entry(a.pk, None)
        emptied = move_entry(b.pk, None)

        self.assertIsNone(result)
        self.assertEqual(emptied, group.pk)
        self.assertFalse(ExternalTagGroup.objects.filter(pk=group.pk).exists())

    def test_moving_between_two_groups_leaves_the_source_group_intact_when_not_emptied(self):
        a = ExternalTagVocabularyEntry.objects.create(source=ExternalTagSource.OSM, key="amenity", value="restaurant")
        b = ExternalTagVocabularyEntry.objects.create(source=ExternalTagSource.OSM, key="cuisine", value="italian")
        group_a = create_group([a.pk, b.pk])
        c = ExternalTagVocabularyEntry.objects.create(source=ExternalTagSource.OVERTURE, key="building_subtype", value="restaurant")
        group_b = create_group([c.pk])

        emptied = move_entry(a.pk, group_b.pk)

        a.refresh_from_db()
        self.assertIsNone(emptied)
        self.assertEqual(a.group_id, group_b.pk)
        self.assertTrue(ExternalTagGroup.objects.filter(pk=group_a.pk).exists())

    def test_moving_the_last_member_between_two_groups_deletes_the_now_empty_source(self):
        a = ExternalTagVocabularyEntry.objects.create(source=ExternalTagSource.OSM, key="amenity", value="restaurant")
        group_a = create_group([a.pk])
        b = ExternalTagVocabularyEntry.objects.create(source=ExternalTagSource.OVERTURE, key="building_subtype", value="restaurant")
        group_b = create_group([b.pk])

        emptied = move_entry(a.pk, group_b.pk)

        self.assertEqual(emptied, group_a.pk)
        self.assertFalse(ExternalTagGroup.objects.filter(pk=group_a.pk).exists())

    def test_dropping_back_into_the_same_group_is_a_no_op(self):
        a = ExternalTagVocabularyEntry.objects.create(source=ExternalTagSource.OSM, key="amenity", value="restaurant")
        b = ExternalTagVocabularyEntry.objects.create(source=ExternalTagSource.OVERTURE, key="building_subtype", value="restaurant")
        group = create_group([a.pk, b.pk])

        result = move_entry(a.pk, group.pk)

        self.assertIsNone(result)
        self.assertTrue(ExternalTagGroup.objects.filter(pk=group.pk).exists())

    def test_removing_a_non_last_member_keeps_the_remaining_singleton(self):
        a = ExternalTagVocabularyEntry.objects.create(source=ExternalTagSource.OSM, key="amenity", value="restaurant")
        b = ExternalTagVocabularyEntry.objects.create(source=ExternalTagSource.OVERTURE, key="building_subtype", value="restaurant")
        group = create_group([a.pk, b.pk])

        move_entry(a.pk, None)

        self.assertTrue(ExternalTagGroup.objects.filter(pk=group.pk).exists())
        b.refresh_from_db()
        self.assertEqual(b.group_id, group.pk)

    def test_unknown_entry_is_refused(self):
        with self.assertRaises(ExternalTagGroupError):
            move_entry(999_999, None)

    def test_unknown_target_group_is_refused(self):
        a = ExternalTagVocabularyEntry.objects.create(source=ExternalTagSource.OSM, key="amenity", value="restaurant")

        with self.assertRaises(ExternalTagGroupError):
            move_entry(a.pk, 999_999)


class SetPreferredTests(TestCase):
    def test_changes_which_member_is_preferred(self):
        a = ExternalTagVocabularyEntry.objects.create(source=ExternalTagSource.OSM, key="amenity", value="restaurant")
        b = ExternalTagVocabularyEntry.objects.create(source=ExternalTagSource.OVERTURE, key="building_subtype", value="restaurant")
        group = create_group([a.pk, b.pk])

        set_preferred(b.pk, group.pk)

        a.refresh_from_db()
        b.refresh_from_db()
        self.assertFalse(a.is_preferred)
        self.assertTrue(b.is_preferred)

    def test_an_entry_not_in_the_given_group_is_refused(self):
        a = ExternalTagVocabularyEntry.objects.create(source=ExternalTagSource.OSM, key="amenity", value="restaurant")
        b = ExternalTagVocabularyEntry.objects.create(source=ExternalTagSource.OVERTURE, key="building_subtype", value="restaurant")
        create_group([a.pk])
        other_group = create_group([b.pk])

        with self.assertRaises(ExternalTagGroupError):
            set_preferred(a.pk, other_group.pk)


class VisibleExternalTagsFilterTests(TestCase):
    """templatetags.dashboard_tags.visible_external_tags - the wiki-facing entry point."""

    def test_none_place_returns_empty_list(self):
        from urbanlens.dashboard.templatetags.dashboard_tags import visible_external_tags

        self.assertEqual(visible_external_tags(None), [])

    def test_delegates_to_visible_tags_for_place(self):
        from urbanlens.dashboard.templatetags.dashboard_tags import visible_external_tags

        place = baker.make(Place)
        _sync(place, ExternalTagSource.OSM, "amenity", "restaurant")
        PlaceExternalTag.objects.bulk_create([PlaceExternalTag(place=place, source=ExternalTagSource.OVERTURE, key="building_subtype", value="restaurant")])
        ExternalTagVocabularyEntry.objects.get_or_create(source=ExternalTagSource.OVERTURE, key="building_subtype", value="restaurant")

        self.assertEqual(len(visible_external_tags(place)), 1)


class VocabularyAutoRegistrationTests(TestCase):
    def test_sync_registers_new_vocabulary_entries(self):
        place = baker.make(Place)

        PlaceExternalTag.sync_for_source(place, ExternalTagSource.OSM, [ExtractedTag(key="amenity", value="restaurant", is_primary=True)])

        self.assertTrue(ExternalTagVocabularyEntry.objects.for_tag(ExternalTagSource.OSM, "amenity", "restaurant").exists())

    def test_resync_does_not_touch_an_existing_entrys_group_or_preference(self):
        place = baker.make(Place)
        PlaceExternalTag.sync_for_source(place, ExternalTagSource.OSM, [ExtractedTag(key="amenity", value="restaurant", is_primary=True)])
        entry = ExternalTagVocabularyEntry.objects.for_tag(ExternalTagSource.OSM, "amenity", "restaurant").get()
        group = create_group([entry.pk])

        PlaceExternalTag.sync_for_source(place, ExternalTagSource.OSM, [ExtractedTag(key="amenity", value="restaurant", is_primary=True)])

        entry.refresh_from_db()
        self.assertEqual(entry.group_id, group.pk)
        self.assertTrue(entry.is_preferred)
