"""Building provenance survives REData's move to a reconciled response.

REData now reconciles `/parcels/{uuid}/buildings/` into one record per physical
building (its `docs/buildings-dedup-spec.md`), which removed the top-level
`source` string a per-observation record used to carry and replaced it with a
`sources[]` array - one entry per source referencing that building, ordered by
`BUILDING_SOURCES` precedence.

Both UrbanLens consumers still read the removed key, and neither fails loudly:
the buildings table's source chip and the building-attributes card's chip just
go blank, which reads as "we don't know where this came from" rather than as a
version skew.

Both shapes have to work at once. The flat one is not legacy - Overpass answers
in it (`parcel_buildings` falls back to Overpass whenever REData has no
buildings for a parcel, which is the path the reported HRSH pin was on).
"""

from __future__ import annotations

from urbanlens.core.tests.testcase import SimpleTestCase
from urbanlens.dashboard.plugins.builtin.parcel_buildings import building_rows, record_sources


class RecordSourcesTests(SimpleTestCase):
    def test_a_reconciled_record_reports_every_source(self) -> None:
        """The point of reconciliation: one building, several sources."""
        record = {"sources": [{"source": "cris"}, {"source": "osm"}]}

        self.assertEqual(record_sources(record), ["cris", "osm"])

    def test_precedence_order_is_preserved(self) -> None:
        """REData orders `sources[]` richest-first; re-sorting would lose that."""
        record = {"sources": [{"source": "county_gis"}, {"source": "cris"}, {"source": "osm"}]}

        self.assertEqual(record_sources(record), ["county_gis", "cris", "osm"])

    def test_a_flat_record_still_works(self) -> None:
        """Overpass-shaped rows never had `sources[]`."""
        self.assertEqual(record_sources({"source": "osm"}), ["osm"])

    def test_an_unsourced_record_is_empty_not_blank_stringed(self) -> None:
        self.assertEqual(record_sources({}), [])
        self.assertEqual(record_sources({"source": ""}), [])

    def test_malformed_entries_are_skipped(self) -> None:
        """A source entry without a `source` key must not become an empty chip."""
        self.assertEqual(record_sources({"sources": [{"name": "x"}, {"source": "cris"}]}), ["cris"])

    def test_an_empty_sources_array_falls_back(self) -> None:
        """`sources: []` alongside a flat key must not lose the flat key."""
        self.assertEqual(record_sources({"sources": [], "source": "osm"}), ["osm"])


class BuildingRowSourceLabelTests(SimpleTestCase):
    """The rendered row, not just the helper."""

    def _row(self, building: dict) -> dict:
        rows = building_rows([building], [])
        self.assertEqual(len(rows), 1)
        return rows[0]

    def test_a_reconciled_building_is_labelled_for_both_sources(self) -> None:
        row = self._row({"name": "Main Building", "sources": [{"source": "cris"}, {"source": "osm"}]})

        self.assertEqual(row["source"], "cris", "the richest source stays the row's primary, which is what sorting and the API use")
        self.assertEqual(row["source_label"], "NY SHPO (CRIS) + OpenStreetMap")

    def test_a_flat_building_is_labelled_as_before(self) -> None:
        row = self._row({"name": "Shed", "source": "osm"})

        self.assertEqual(row["source"], "osm")
        self.assertEqual(row["source_label"], "OpenStreetMap")

    def test_an_unknown_source_key_produces_no_chip(self) -> None:
        """A source REData adds later must not render a raw key at the user."""
        row = self._row({"name": "Shed", "sources": [{"source": "some_new_source"}]})

        self.assertEqual(row["source_label"], "")

    def test_every_source_redata_can_return_is_labelled(self) -> None:
        """A missing label is silent: the chip just does not render.

        REData's `BUILDING_SOURCES` has six entries and four of them had no
        label here, including `overpass` - which is in its *default* set, so
        the most common REData-sourced building on any parcel outside NY
        rendered with no provenance at all.
        """
        for key in ("county_gis", "assessor", "cris", "overpass", "microsoft_buildings", "google_open_buildings"):
            with self.subTest(source=key):
                row = self._row({"name": "Shed", "sources": [{"source": key}]})

                self.assertNotEqual(row["source_label"], "", f"{key} renders no provenance chip")

    def test_overpass_and_osm_share_one_label_without_repeating_it(self) -> None:
        """They are the same data under REData's tag and this plugin's fallback tag."""
        row = self._row({"name": "Shed", "sources": [{"source": "overpass"}, {"source": "osm"}]})

        self.assertEqual(row["source_label"], "OpenStreetMap")


class BuildingsOnPropertyTests(SimpleTestCase):
    """REData labels what it over-returns; ignoring the label is how 2604 happened.

    A parcel inside a broad CRIS archaeological sensitivity zone gets every
    surveyed building in that zone, each flagged ``is_on_property: false``. The
    campus's own survey roster is 124.
    """

    def test_off_property_records_are_dropped(self) -> None:
        from urbanlens.dashboard.plugins.builtin.parcel_buildings import buildings_on_property

        kept = buildings_on_property([{"name": "on", "is_on_property": True}, {"name": "off", "is_on_property": False}])

        self.assertEqual([b["name"] for b in kept], ["on"])

    def test_an_absent_flag_is_not_a_negative_one(self) -> None:
        """Overpass rows carry no flag at all and must survive."""
        from urbanlens.dashboard.plugins.builtin.parcel_buildings import buildings_on_property

        self.assertEqual(len(buildings_on_property([{"name": "osm row", "source": "osm"}])), 1)

    def test_a_parent_is_kept_in_the_list(self) -> None:
        """A building containing others is still a building.

        The Kirkbride case: a large building whose wings are separately mapped
        parents them, while remaining the structure the site is named after.
        Filtering it out would delete the most significant building on a campus.
        """
        from urbanlens.dashboard.plugins.builtin.parcel_buildings import buildings_on_property

        kept = buildings_on_property(_NESTED)

        self.assertEqual([b["ref"] for b in kept], ["osm:way/552009229", "cris:1", "cris:2"])

    def test_a_parent_is_not_counted_alongside_its_children(self) -> None:
        """Counting the container as well as its contents double-counts them."""
        from urbanlens.dashboard.plugins.builtin.parcel_buildings import countable_buildings

        self.assertEqual([b["ref"] for b in countable_buildings(_NESTED)], ["cris:1", "cris:2"])

    def test_counting_still_excludes_off_property_records(self) -> None:
        from urbanlens.dashboard.plugins.builtin.parcel_buildings import countable_buildings

        records = [*_NESTED, {"ref": "cris:99", "is_on_property": False}]

        self.assertNotIn("cris:99", [b["ref"] for b in countable_buildings(records)])

    def test_the_panel_rows_are_filtered(self) -> None:
        rows = building_rows([{"name": "on", "is_on_property": True}, {"name": "off", "is_on_property": False}], [])

        self.assertEqual([r["name"] for r in rows], ["on"])


_NESTED = [
    {"ref": "osm:way/552009229", "name": "Kirkbride", "child_refs": ["cris:1", "cris:2"]},
    {"ref": "cris:1", "name": "North Wing", "parent_ref": "osm:way/552009229"},
    {"ref": "cris:2", "name": "South Wing", "parent_ref": "osm:way/552009229"},
]


class BuildingNestingTests(SimpleTestCase):
    """Nesting is reported by REData, not inferred from geometry here.

    It is a tree of arbitrary depth (a campus block parenting a wing parenting
    an annex), and it is not always cross-source - OSM models a `building`
    outline over its own `building:part` segments.
    """

    def test_children_follow_their_parent(self) -> None:
        rows = building_rows(_NESTED, [])

        self.assertEqual([r["name"] for r in rows], ["Kirkbride", "North Wing", "South Wing"])

    def test_depth_marks_the_nesting_level(self) -> None:
        rows = building_rows(_NESTED, [])

        self.assertEqual([r["depth"] for r in rows], [0, 1, 1])

    def test_nesting_can_be_more_than_one_level_deep(self) -> None:
        """A child links to its most specific parent, which may itself be a child."""
        records = [
            {"ref": "block", "name": "Block", "child_refs": ["wing"]},
            {"ref": "wing", "name": "Wing", "parent_ref": "block", "child_refs": ["annex"]},
            {"ref": "annex", "name": "Annex", "parent_ref": "wing"},
        ]

        rows = building_rows(records, [])

        self.assertEqual([r["name"] for r in rows], ["Block", "Wing", "Annex"])
        self.assertEqual([r["depth"] for r in rows], [0, 1, 2])

    def test_same_source_nesting_is_honoured(self) -> None:
        """An OSM outline over its own building:part segments is one source."""
        records = [
            {"ref": "osm:way/1", "name": "Outline", "sources": [{"source": "osm"}], "child_refs": ["osm:way/2"]},
            {"ref": "osm:way/2", "name": "Part", "sources": [{"source": "osm"}], "parent_ref": "osm:way/1"},
        ]

        self.assertEqual([r["depth"] for r in building_rows(records, [])], [0, 1])

    def test_a_parent_outside_the_list_leaves_the_child_visible(self) -> None:
        """Its parent may have been dropped as off-property; the child is not lost."""
        records = [{"ref": "cris:1", "name": "Orphan", "parent_ref": "gone"}]

        rows = building_rows(records, [])

        self.assertEqual([r["name"] for r in rows], ["Orphan"])
        self.assertEqual(rows[0]["depth"], 0)

    def test_a_parent_ref_cycle_does_not_hang_or_drop_rows(self) -> None:
        records = [
            {"ref": "a", "name": "A", "parent_ref": "b"},
            {"ref": "b", "name": "B", "parent_ref": "a"},
        ]

        self.assertEqual(sorted(r["name"] for r in building_rows(records, [])), ["A", "B"])

    def test_flat_records_are_still_sorted_by_building_number(self) -> None:
        """Nesting must not disturb the numeric ordering people navigate by."""
        records = [{"building_number": "10", "name": "ten"}, {"building_number": "9", "name": "nine"}]

        self.assertEqual([r["name"] for r in building_rows(records, [])], ["nine", "ten"])
