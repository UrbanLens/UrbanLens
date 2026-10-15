"""Historic-register listings are read through the HISTORIC_REGISTERS seam: names, years and the rows that refresh them."""

from __future__ import annotations

from typing import Any, ClassVar
from unittest import mock

from model_bakery import baker

from urbanlens.core.tests.testcase import SimpleTestCase, TestCase
from urbanlens.dashboard.models.cache.location_cache import LocationCache
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.plugins.points import HISTORIC_REGISTERS, NAME_PROVIDERS
from urbanlens.dashboard.services.locations.historic_registers import (
    HistoricRegisterNameProvider,
    HistoricRegisterSource,
    names_a_new_building,
    register_cache_sources,
    register_listing_names,
    register_listing_year,
)
from urbanlens.dashboard.services.locations.name_tiers import NameTier, tier_for


class _Register(HistoricRegisterSource):
    """A register answering fixed listings."""

    cache_sources: ClassVar[frozenset[str]] = frozenset({"register_rows"})

    def __init__(self, names: list[str], year: int | None = None, *, new_building: bool = False) -> None:
        self._names = names
        self._year = year
        self._new_building = new_building

    def listing_names(self, location: Location) -> list[str]:
        return self._names

    def listing_year(self, location: Location) -> int | None:
        return self._year

    def names_new_building(self, row: Any) -> bool:
        return self._new_building


class RegisterChainTests(SimpleTestCase):
    def test_names_come_from_every_register_in_order(self) -> None:
        with HISTORIC_REGISTERS.overridden([_Register(["Hall Listing"]), _Register(["District"])]):
            self.assertEqual(register_listing_names(Location()), ["Hall Listing", "District"])

    def test_no_register_lists_nothing(self) -> None:
        with HISTORIC_REGISTERS.overridden([]):
            self.assertEqual(register_listing_names(Location()), [])
            self.assertIsNone(register_listing_year(Location()))
            self.assertEqual(register_cache_sources(), frozenset())

    def test_the_first_register_that_dates_the_property_gives_the_year(self) -> None:
        with HISTORIC_REGISTERS.overridden([_Register([], None), _Register([], 1892), _Register([], 1901)]):
            self.assertEqual(register_listing_year(Location()), 1892)

    def test_the_cache_sources_are_those_of_all_registers(self) -> None:
        other = _Register([])
        other.cache_sources = frozenset({"more_rows"})
        with HISTORIC_REGISTERS.overridden([_Register([]), other]):
            self.assertEqual(register_cache_sources(), frozenset({"register_rows", "more_rows"}))

    def test_a_row_names_a_new_building_only_for_the_register_that_owns_its_source(self) -> None:
        row = mock.Mock(source="register_rows")
        with HISTORIC_REGISTERS.overridden([_Register([], new_building=True)]):
            self.assertTrue(names_a_new_building(row))
        with HISTORIC_REGISTERS.overridden([_Register([], new_building=False)]):
            self.assertFalse(names_a_new_building(row))
        stranger = mock.Mock(source="unrelated")
        with HISTORIC_REGISTERS.overridden([_Register([], new_building=True)]):
            self.assertFalse(names_a_new_building(stranger))


class RegisterNameProviderTests(SimpleTestCase):
    def test_its_candidates_are_the_listings_and_it_ranks_as_a_register(self) -> None:
        provider = HistoricRegisterNameProvider()
        with HISTORIC_REGISTERS.overridden([_Register(["Hall Listing"])]), NAME_PROVIDERS.overridden([provider]):
            self.assertEqual(provider.candidates(Location()), ["Hall Listing"])
            self.assertEqual(tier_for("historic_register", None), NameTier.HISTORIC_REGISTER)


class RegisterRowRefreshesNamesTests(TestCase):
    """A saved row of a register's cache source refreshes the location's names; one of another source does not."""

    def setUp(self) -> None:
        super().setUp()
        self.location = baker.make(Location, latitude=41.7, longitude=-73.9, google_place=None)

    def _save(self, source: str) -> mock.Mock:
        with (
            HISTORIC_REGISTERS.overridden([_Register(["Hall Listing"])]),
            mock.patch(
                "urbanlens.dashboard.services.locations.naming.update_location_name_from_external_sources"
            ) as update,
            self.captureOnCommitCallbacks(execute=True),
        ):
            LocationCache.set(self.location, source, {"anything": 1}, query_key="q")
        return update

    def test_a_register_row_refreshes_the_names(self) -> None:
        self.assertTrue(self._save("register_rows").called)

    def test_a_row_of_another_source_does_not(self) -> None:
        self.assertFalse(self._save("unrelated_rows").called)
