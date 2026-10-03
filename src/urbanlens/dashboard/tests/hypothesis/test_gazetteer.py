"""The gazetteer's bundled data is what the installed geonamescache holds, and reads back as places."""

from __future__ import annotations

import json
import lzma

from django.test import SimpleTestCase

from urbanlens.dashboard.services.geo import build_gazetteer, gazetteer

_REBUILD = "the bundled data is stale: run `bun run gazetteer:build`"


class BundledDataTests(SimpleTestCase):
    def test_the_cities_are_what_geonamescache_holds(self) -> None:
        with lzma.open(gazetteer.CITIES_FILE, "rt", encoding="utf-8") as file:
            bundled = [line.rstrip("\n") for line in file if not line.startswith("#")]
        self.assertEqual(bundled, build_gazetteer.city_lines(), _REBUILD)

    def test_the_regions_are_what_geonamescache_holds(self) -> None:
        self.assertEqual(
            json.loads(gazetteer.REGIONS_FILE.read_text(encoding="utf-8")), build_gazetteer.regions(), _REBUILD
        )

    def test_the_files_name_their_source_and_licence(self) -> None:
        with lzma.open(gazetteer.CITIES_FILE, "rt", encoding="utf-8") as file:
            self.assertEqual(file.readline().rstrip("\n"), f"# {build_gazetteer.ATTRIBUTION}")
        self.assertEqual(
            json.loads(gazetteer.REGIONS_FILE.read_text(encoding="utf-8"))["attribution"], build_gazetteer.ATTRIBUTION
        )


class LookupTests(SimpleTestCase):
    def test_a_city_reads_back_with_its_state_and_position(self) -> None:
        (city,) = [city for city in gazetteer.cities_named("poughkeepsie") if city.country_code == "US"]
        self.assertEqual(city.admin1, "NY")
        self.assertLess(city.km_from(41.7333, -73.9281), 5)

    def test_a_name_shared_by_several_cities_returns_each(self) -> None:
        states = {city.admin1 for city in gazetteer.cities_named("salem") if city.country_code == "US"}
        self.assertTrue({"OR", "MA"} <= states)

    def test_regions_read_back_by_folded_name(self) -> None:
        self.assertEqual(gazetteer.us_states()["new york"], "NY")
        self.assertEqual(gazetteer.us_states()["district of columbia"], "DC")
        self.assertIn("dutchess county", gazetteer.us_counties())
        self.assertEqual(gazetteer.countries()["germany"], "DE")
        self.assertEqual(gazetteer.country_names_by_code()["US"], "United States")

    def test_the_nearest_city_is_found_only_within_range(self) -> None:
        nearest = gazetteer.nearest_city(41.73, -73.93, within_km=50)
        self.assertIsNotNone(nearest)
        assert nearest is not None
        self.assertEqual((nearest.country_code, nearest.admin1), ("US", "NY"))
        self.assertIsNone(gazetteer.nearest_city(0.0, -30.0, within_km=50))
