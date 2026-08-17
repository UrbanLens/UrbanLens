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
