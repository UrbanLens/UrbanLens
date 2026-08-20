"""What the parcel record already knew about where you would be standing.

REData resolves four Census Special Land Use Area categories on every parcel
fetch - national park, correctional facility, college/university, military
installation - by point-in-polygon against TIGERweb's own layer, and UrbanLens
cached the answer and rendered none of it. Alongside it sat `flood_zone_code`
and `deed_document_links`, fetched and equally unread.

For this application the land-use categories are not another attribute of the
property. Two of them describe ground where being present is a different
statute rather than a trespass question, and the record had already said so.
These tests pin that the panel says it too, and says it prominently - in the
chips, before the tax and valuation detail, not buried at the end of a
definition list.
"""

from __future__ import annotations

from urbanlens.core.tests.testcase import SimpleTestCase
from urbanlens.dashboard.plugins.builtin.property_records import _render_available, special_land_use_rows


def _area(name: str) -> dict[str, str]:
    return {"name": name, "geoid": "123"}


class SpecialLandUseRowTests(SimpleTestCase):
    def test_the_common_case_is_no_rows(self) -> None:
        """A parcel inside none of the four gets `{}` from REData."""
        self.assertEqual(special_land_use_rows({}), [])

    def test_access_controlled_categories_come_first(self) -> None:
        """Order is by consequence, not by REData's key order or the alphabet."""
        rows = special_land_use_rows(
            {
                "college_university": _area("State University"),
                "national_park": _area("Yellowstone"),
                "correctional_facility": _area("State Penitentiary"),
                "military_installation": _area("Fort Example"),
            },
        )

        self.assertEqual([row["category"] for row in rows], ["military_installation", "correctional_facility", "national_park", "college_university"])

    def test_a_category_the_parcel_is_outside_is_skipped(self) -> None:
        """REData reports every category, with None for the ones that missed."""
        rows = special_land_use_rows({"national_park": None, "military_installation": _area("Fort Example")})

        self.assertEqual([row["name"] for row in rows], ["Fort Example"])

    def test_an_unnamed_area_still_produces_a_row(self) -> None:
        """*That* you are inside one matters whether or not the layer names it.

        TIGERweb rows are confirmed to omit fields per category, so a missing
        name is expected data, not corruption - and dropping the row would turn
        "inside a correctional facility" into silence.
        """
        rows = special_land_use_rows({"correctional_facility": {"geoid": "9"}})

        self.assertEqual(rows, [{"category": "correctional_facility", "label": "Correctional facility", "name": "Correctional facility"}])

    def test_malformed_input_does_not_raise(self) -> None:
        self.assertEqual(special_land_use_rows(None), [])
        self.assertEqual(special_land_use_rows(["not", "a", "dict"]), [])
        self.assertEqual(special_land_use_rows({"military_installation": "Fort Example"}), [{"category": "military_installation", "label": "Military installation", "name": "Military installation"}])


class PropertyCardRenderTests(SimpleTestCase):
    def _context(self, **extra) -> dict:
        return _render_available({"available": True, "situs_address": "1 Main St", **extra}, show_owner=False)

    def test_a_restricted_parcel_is_chipped_not_only_listed(self) -> None:
        """A row halfway down a definition list is not a warning."""
        context = self._context(special_land_use_areas={"military_installation": _area("Fort Example")})

        self.assertEqual(context["chips"][0], "Military installation")
        values = {entry["label"]: entry["value"] for entry in context["meta"]}
        self.assertEqual(values["Military installation"], "Fort Example")

    def test_land_use_chips_precede_the_property_condition_chips(self) -> None:
        """"Delinquent taxes" is a fact about the property; this is about the visit."""
        context = self._context(
            special_land_use_areas={"correctional_facility": _area("State Penitentiary")},
            tax_history=[{"delinquent": True}],
            parcel_geometry={"type": "Polygon"},
        )

        self.assertEqual(context["chips"], ["Correctional facility", "Delinquent taxes", "Boundary available"])

    def test_an_ordinary_parcel_gains_no_chips(self) -> None:
        self.assertEqual(self._context(special_land_use_areas={}, flood_zone_code="")["chips"], [])

    def test_the_flood_zone_is_shown(self) -> None:
        values = {entry["label"]: entry["value"] for entry in self._context(flood_zone_code="AE")["meta"]}

        self.assertEqual(values["Flood zone"], "AE")

    def test_recorded_documents_are_linked(self) -> None:
        context = self._context(deed_document_links=["https://recorder.example/deed/1"])

        links = [entry for entry in context["meta"] if entry.get("href")]
        self.assertEqual(links, [{"label": "Recorded document", "value": "View document", "href": "https://recorder.example/deed/1"}])

    def test_a_long_document_list_is_truncated_and_numbered(self) -> None:
        context = self._context(deed_document_links=[f"https://recorder.example/deed/{index}" for index in range(20)])

        links = [entry for entry in context["meta"] if entry.get("href")]
        self.assertEqual(len(links), 5)
        self.assertEqual(links[1]["label"], "Recorded document 2")

    def test_blank_and_non_string_document_entries_are_skipped(self) -> None:
        context = self._context(deed_document_links=["", "   ", None, 7, "https://recorder.example/deed/1"])

        links = [entry for entry in context["meta"] if entry.get("href")]
        self.assertEqual(links, [{"label": "Recorded document", "value": "View document", "href": "https://recorder.example/deed/1"}], "numbering follows displayed position, not source position")

    def test_a_record_without_any_of_these_fields_renders_unchanged(self) -> None:
        """Every one of them is optional; a sparse county record must not break."""
        context = self._context()

        self.assertEqual(context["chips"], [])
        self.assertEqual(context["meta"], [{"label": "Address", "value": "1 Main St"}])
