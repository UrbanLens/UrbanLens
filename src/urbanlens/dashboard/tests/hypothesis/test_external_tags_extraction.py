"""Tests for extract_nominatim_tags/extract_overture_tags - pure functions, no DB.

Fixture dicts mirror the shapes NominatimGateway._normalise() and
OvertureMapsGateway.get_building_attributes() actually return (the former is
also exactly what LocationCache(source="nominatim").data holds), not
generated fake data - the fallback/exclusion rules under test are about
specific field-name relationships in those real shapes, not arbitrary dicts.
"""

from __future__ import annotations

from urbanlens.core.tests.testcase import SimpleTestCase
from urbanlens.dashboard.models.place.external_tag import ExtractedTag
from urbanlens.dashboard.services.locations.external_tags import extract_nominatim_tags, extract_overture_tags, humanize_tag_value


class ExtractNominatimTagsTests(SimpleTestCase):
    def test_primary_category_type_pair_is_marked_primary(self):
        tags = extract_nominatim_tags({"category": "amenity", "type": "restaurant"})

        self.assertEqual(tags, [ExtractedTag(key="amenity", value="restaurant", is_primary=True)])

    def test_secondary_field_kept_when_it_adds_information(self):
        tags = extract_nominatim_tags({"category": "tourism", "type": "museum", "historic": "yes"})

        self.assertIn(ExtractedTag(key="tourism", value="museum", is_primary=True), tags)
        self.assertIn(ExtractedTag(key="historic", value="yes", is_primary=False), tags)
        self.assertEqual(len(tags), 2)

    def test_secondary_field_identical_to_primary_is_skipped(self):
        tags = extract_nominatim_tags({"category": "amenity", "type": "restaurant", "amenity": "restaurant"})

        self.assertEqual(tags, [ExtractedTag(key="amenity", value="restaurant", is_primary=True)])

    def test_all_four_secondary_fields_can_be_kept_independently(self):
        tags = extract_nominatim_tags(
            {
                "category": "leisure",
                "type": "park",
                "building": "yes",
                "amenity": "playground",
                "tourism": "attraction",
                "historic": "yes",
            },
        )

        keys = {tag.key for tag in tags}
        self.assertEqual(keys, {"leisure", "building", "amenity", "tourism", "historic"})

    def test_missing_type_produces_no_primary_but_keeps_secondary(self):
        tags = extract_nominatim_tags({"category": "amenity", "building": "house"})

        self.assertEqual(tags, [ExtractedTag(key="building", value="house", is_primary=False)])

    def test_empty_dict_produces_no_tags(self):
        self.assertEqual(extract_nominatim_tags({}), [])

    def test_nearby_places_key_is_not_a_field_this_function_reads(self):
        # Nominatim's normalised dict never carries "nearby_places" (that's an
        # Overture concept) - passing one anyway must not surface it as a tag.
        tags = extract_nominatim_tags({"category": "amenity", "type": "cafe", "nearby_places": [{"category": "bakery"}]})

        self.assertEqual(tags, [ExtractedTag(key="amenity", value="cafe", is_primary=True)])


class ExtractOvertureTagsTests(SimpleTestCase):
    def test_subtype_is_primary(self):
        tags = extract_overture_tags({"subtype": "single_family_residential", "class_": "residential"})

        self.assertIn(ExtractedTag(key="building_subtype", value="single_family_residential", is_primary=True), tags)
        self.assertIn(ExtractedTag(key="building_class", value="residential", is_primary=False), tags)
        self.assertEqual(len(tags), 2)

    def test_class_identical_to_subtype_is_skipped(self):
        tags = extract_overture_tags({"subtype": "residential", "class_": "residential"})

        self.assertEqual(tags, [ExtractedTag(key="building_subtype", value="residential", is_primary=True)])

    def test_class_alone_is_promoted_to_primary(self):
        tags = extract_overture_tags({"class_": "residential"})

        self.assertEqual(tags, [ExtractedTag(key="building_class", value="residential", is_primary=True)])

    def test_empty_dict_produces_no_tags(self):
        self.assertEqual(extract_overture_tags({}), [])

    def test_nearby_places_is_never_read(self):
        # The merged {**attributes, "nearby_places": [...]} shape the panel
        # builds for its cache row must never be passed here in real usage -
        # this proves that even if it were, nearby_places contributes nothing.
        tags = extract_overture_tags(
            {
                "subtype": "single_family_residential",
                "nearby_places": [{"category": "italian_restaurant", "distance_m": 12.0}],
            },
        )

        self.assertEqual(tags, [ExtractedTag(key="building_subtype", value="single_family_residential", is_primary=True)])


class HumanizeTagValueTests(SimpleTestCase):
    def test_boolish_value_maps_to_friendly_label(self):
        self.assertEqual(humanize_tag_value("yes"), "Yes")
        self.assertEqual(humanize_tag_value("limited"), "Limited")

    def test_snake_case_value_becomes_spaced(self):
        self.assertEqual(humanize_tag_value("single_family_residential"), "single family residential")

    def test_semicolon_separated_value_becomes_comma_joined(self):
        self.assertEqual(humanize_tag_value("fine_dining;regional"), "fine dining, regional")
