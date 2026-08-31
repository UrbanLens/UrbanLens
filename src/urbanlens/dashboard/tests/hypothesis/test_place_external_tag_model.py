"""Tests for PlaceExternalTag.sync_for_source/is_fresh_for - the Place-scoped external-tag store."""

from __future__ import annotations

from datetime import timedelta

from django.db import IntegrityError
from django.utils import timezone
from hypothesis import given, settings as hyp_settings, strategies as st
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.place.external_tag import ExternalTagSource, ExtractedTag, PlaceExternalTag
from urbanlens.dashboard.models.place.model import Place
from urbanlens.dashboard.models.site_settings.model import SiteSettings

_hyp = hyp_settings(max_examples=30, deadline=None)


class SyncForSourceTests(TestCase):
    def test_sync_creates_rows_for_a_place(self):
        place = baker.make(Place)

        PlaceExternalTag.sync_for_source(place, ExternalTagSource.OSM, [ExtractedTag(key="amenity", value="restaurant", is_primary=True)])

        self.assertEqual(list(place.external_tags.values_list("key", "value")), [("amenity", "restaurant")])

    def test_sync_replaces_only_that_sources_prior_rows(self):
        place = baker.make(Place)
        PlaceExternalTag.sync_for_source(place, ExternalTagSource.OSM, [ExtractedTag(key="amenity", value="restaurant", is_primary=True)])
        PlaceExternalTag.sync_for_source(place, ExternalTagSource.OVERTURE, [ExtractedTag(key="building_subtype", value="restaurant", is_primary=True)])

        PlaceExternalTag.sync_for_source(place, ExternalTagSource.OSM, [ExtractedTag(key="shop", value="bakery", is_primary=True)])

        osm_tags = set(place.external_tags.filter(source=ExternalTagSource.OSM).values_list("key", "value"))
        overture_tags = set(place.external_tags.filter(source=ExternalTagSource.OVERTURE).values_list("key", "value"))
        self.assertEqual(osm_tags, {("shop", "bakery")})
        self.assertEqual(overture_tags, {("building_subtype", "restaurant")})

    def test_second_sync_with_different_tags_leaves_no_stale_rows(self):
        place = baker.make(Place)
        PlaceExternalTag.sync_for_source(place, ExternalTagSource.OSM, [ExtractedTag(key="amenity", value="restaurant", is_primary=True)])

        PlaceExternalTag.sync_for_source(place, ExternalTagSource.OSM, [ExtractedTag(key="amenity", value="cafe", is_primary=True)])

        self.assertEqual(list(place.external_tags.values_list("key", "value")), [("amenity", "cafe")])

    def test_empty_value_tags_are_skipped(self):
        place = baker.make(Place)

        PlaceExternalTag.sync_for_source(place, ExternalTagSource.OSM, [ExtractedTag(key="amenity", value="", is_primary=True)])

        self.assertFalse(place.external_tags.exists())

    def test_unique_constraint_holds_within_one_sync(self):
        place = baker.make(Place)
        duplicate_tags = [
            ExtractedTag(key="amenity", value="restaurant", is_primary=True),
            ExtractedTag(key="amenity", value="restaurant", is_primary=False),
        ]

        with self.assertRaises(IntegrityError):
            PlaceExternalTag.sync_for_source(place, ExternalTagSource.OSM, duplicate_tags)


class IsFreshForTests(TestCase):
    def test_false_with_no_rows(self):
        place = baker.make(Place)

        self.assertFalse(PlaceExternalTag.is_fresh_for(place, ExternalTagSource.OSM))

    def test_true_immediately_after_a_sync(self):
        place = baker.make(Place)
        PlaceExternalTag.sync_for_source(place, ExternalTagSource.OSM, [ExtractedTag(key="amenity", value="restaurant", is_primary=True)])

        self.assertTrue(PlaceExternalTag.is_fresh_for(place, ExternalTagSource.OSM))

    def test_false_once_older_than_the_configured_window(self):
        place = baker.make(Place)
        PlaceExternalTag.sync_for_source(place, ExternalTagSource.OSM, [ExtractedTag(key="amenity", value="restaurant", is_primary=True)])
        PlaceExternalTag.objects.filter(place=place, source=ExternalTagSource.OSM).update(updated=timezone.now() - timedelta(days=8))

        self.assertFalse(PlaceExternalTag.is_fresh_for(place, ExternalTagSource.OSM))

    def test_respects_configured_cache_window(self):
        site_settings = SiteSettings.get_current()
        site_settings.external_data_cache_days = 30
        site_settings.save()
        place = baker.make(Place)
        PlaceExternalTag.sync_for_source(place, ExternalTagSource.OSM, [ExtractedTag(key="amenity", value="restaurant", is_primary=True)])
        PlaceExternalTag.objects.filter(place=place, source=ExternalTagSource.OSM).update(updated=timezone.now() - timedelta(days=10))

        self.assertTrue(PlaceExternalTag.is_fresh_for(place, ExternalTagSource.OSM))

    def test_is_independent_per_source(self):
        place = baker.make(Place)
        PlaceExternalTag.sync_for_source(place, ExternalTagSource.OSM, [ExtractedTag(key="amenity", value="restaurant", is_primary=True)])

        self.assertFalse(PlaceExternalTag.is_fresh_for(place, ExternalTagSource.OVERTURE))


class SyncForSourcePropertyTests(TestCase):
    """Property: syncing any small set of distinct (key, value) tags round-trips exactly."""

    @given(
        pairs=st.lists(
            st.tuples(
                st.text(alphabet=st.characters(whitelist_categories=("Ll",)), min_size=1, max_size=10),
                st.text(alphabet=st.characters(whitelist_categories=("Ll",)), min_size=1, max_size=10),
            ),
            min_size=0,
            max_size=8,
            unique=True,
        ),
    )
    @_hyp
    def test_sync_stores_exactly_the_distinct_tags_given(self, pairs: list[tuple[str, str]]):
        place = baker.make(Place)
        tags = [ExtractedTag(key=key, value=value, is_primary=False) for key, value in pairs]

        PlaceExternalTag.sync_for_source(place, ExternalTagSource.OSM, tags)

        stored = set(place.external_tags.values_list("key", "value"))
        self.assertEqual(stored, set(pairs))
        for key, value in pairs:
            self.assertTrue(PlaceExternalTag.objects.for_place(place).matching(key, value).exists())
