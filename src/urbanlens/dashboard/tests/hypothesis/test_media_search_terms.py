"""Tests for the shared media-gallery plumbing and each gateway's relevance flags.

Covers MediaPanelSource.search_terms(), MediaProvider.get_media()'s cache
lookup, and the per-provider flags that keep archive searches on-topic - the
fix for LOC/Smithsonian/Internet Archive returning irrelevant nationwide
results for a pin with no real landmark name (just its street address as a
fallback "name"). See
services.apis.locations.redata_reference_documents_gateway.LibraryOfCongressMediaProvider
and services.locations.naming.is_address_derived_name.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

from urbanlens.core.tests.testcase import SimpleTestCase
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.services.apis.assets.base import MediaItem, MediaProvider
from urbanlens.dashboard.services.apis.locations.redata_reference_documents_gateway import (
    InternetArchiveMediaProvider,
    LibraryOfCongressMediaProvider,
    SmithsonianMediaProvider,
)
from urbanlens.dashboard.services.pins.external_data import MediaPanelSource

if TYPE_CHECKING:
    from collections.abc import Generator


@dataclass(slots=True, kw_only=True)
class _BareGateway(MediaProvider):
    """A MediaProvider with every flag left at its base default.

    search_terms() (the only thing exercised here) never makes an HTTP call,
    so the auto-assigned service_key's rate-limiter session wrapper (see
    Gateway.__post_init__) is harmless - it's constructed but never used.
    """

    def _generate_media(self, search_term: str, address: str | None = None) -> Generator[MediaItem, Any, None]:
        yield from ()


@dataclass(slots=True, kw_only=True)
class _RejectingGateway(_BareGateway):
    """A MediaProvider configured like LOC: skip address-derived-only names."""

    reject_address_derived_names = True


@dataclass(slots=True, kw_only=True)
class _NoAddressGateway(_BareGateway):
    """A MediaProvider that never includes the street address in its query."""

    include_address = False


def _location(**kwargs) -> Location:
    return Location(latitude="39.19749", longitude="-84.46964", **kwargs)


def _pin(location: Location) -> Pin:
    pin = Pin()
    pin._state.fields_cache["location"] = location
    return pin


class MediaPanelSourceSearchTermsRejectAddressDerivedTests(SimpleTestCase):
    """search_terms() skips a provider entirely for a pin with only an address-derived name."""

    def test_address_derived_only_name_yields_no_terms_when_rejected(self) -> None:
        """The exact reported scenario: no real landmark name, just the street address."""
        loc = _location(street_number="1265", route="Section Rd", locality="Cincinnati", administrative_area_level_1="OH", official_name="1265 Section Rd")
        pin = _pin(loc)
        self.assertEqual(MediaPanelSource.search_terms(pin, _RejectingGateway()), [])

    def test_address_derived_name_still_searched_when_flag_is_off(self) -> None:
        loc = _location(street_number="1265", route="Section Rd", locality="Cincinnati", administrative_area_level_1="OH", official_name="1265 Section Rd")
        pin = _pin(loc)
        terms = MediaPanelSource.search_terms(pin, _BareGateway())
        self.assertNotEqual(terms, [])

    def test_real_landmark_name_is_not_rejected(self) -> None:
        loc = _location(street_number="42", route="Mill St", locality="Springfield", administrative_area_level_1="IL", official_name="Riverside Mill")
        pin = _pin(loc)
        terms = MediaPanelSource.search_terms(pin, _RejectingGateway())
        self.assertNotEqual(terms, [])
        self.assertIn("Riverside Mill", terms[0])


class MediaPanelSourceSearchTermsIncludeAddressTests(SimpleTestCase):
    """include_address=False drops a genuinely separate street address from the query."""

    def test_include_address_false_omits_the_address_when_name_is_distinct(self) -> None:
        loc = _location(street_number="42", route="Mill St", locality="Springfield", administrative_area_level_1="IL", official_name="Riverside Mill")
        pin = _pin(loc)
        with_address = MediaPanelSource.search_terms(pin, _BareGateway())
        without_address = MediaPanelSource.search_terms(pin, _NoAddressGateway())
        self.assertIn("Mill St", with_address[0])
        self.assertNotIn("Mill St", without_address[0])
        self.assertIn("Riverside Mill", without_address[0])


@dataclass(slots=True, kw_only=True)
class _CountingGateway(_BareGateway):
    """Records every search term it is asked to fetch."""

    fetched: list[str] | None = None

    def _generate_media(self, search_term: str, address: str | None = None) -> Generator[MediaItem, Any, None]:
        if self.fetched is None:
            self.fetched = []
        self.fetched.append(search_term)
        yield MediaItem(url=f"https://example.test/{search_term}", thumb_url="", caption=search_term, source="test")


class MediaProviderCacheKeyTests(SimpleTestCase):
    """get_media() only reuses a cache row written for the *same* query.

    LocationCache.get_fresh judges freshness by age alone, so without this a
    provider whose query construction was tightened for relevance would keep
    serving results fetched by the old, noisy query for the rest of the 7-day
    TTL and the fix would look like it had done nothing.
    """

    class _Row:
        def __init__(self, query_key: str, data: dict) -> None:
            self.query_key = query_key
            self.data = data

    def _run(self, cached_query_key: str, terms: list[str]) -> tuple[list[MediaItem], bool, list[str]]:
        gateway = _CountingGateway()
        row = self._Row(cached_query_key, {"items": [{"url": "https://example.test/cached", "thumb_url": "", "caption": "cached", "source": "test", "page_url": ""}]})
        with (
            patch("urbanlens.dashboard.models.cache.location_cache.LocationCache.get_fresh", return_value=row),
            patch("urbanlens.dashboard.models.cache.location_cache.LocationCache.set") as mock_set,
        ):
            items, from_cache = gateway.get_media(_location(), terms)
        self.assertEqual(mock_set.called, not from_cache)
        return items, from_cache, gateway.fetched or []

    def test_matching_query_key_is_served_from_cache(self) -> None:
        items, from_cache, fetched = self._run('"Riverside Mill" "Springfield IL"', ['"Riverside Mill" "Springfield IL"'])
        self.assertTrue(from_cache)
        self.assertEqual([item.caption for item in items], ["cached"])
        self.assertEqual(fetched, [])

    def test_changed_query_key_refetches_instead_of_serving_stale_results(self) -> None:
        """The upgrade path: a row written by the pre-fix (unquoted) query."""
        items, from_cache, fetched = self._run("Riverside Mill Springfield IL", ['"Riverside Mill" "Springfield IL"'])
        self.assertFalse(from_cache)
        self.assertEqual(fetched, ['"Riverside Mill" "Springfield IL"'])
        self.assertEqual([item.caption for item in items], ['"Riverside Mill" "Springfield IL"'])

    def test_multiple_terms_join_into_one_key(self) -> None:
        _items, from_cache, _fetched = self._run('"A" | "B"', ['"A"', '"B"'])
        self.assertTrue(from_cache)


class LibraryOfCongressMediaProviderRelevanceFlagsTests(SimpleTestCase):
    """Regression guard for LOC's specific configuration."""

    def test_reject_address_derived_names_is_enabled(self) -> None:
        self.assertTrue(LibraryOfCongressMediaProvider.reject_address_derived_names)

    def test_include_address_is_disabled(self) -> None:
        self.assertFalse(LibraryOfCongressMediaProvider.include_address)

    def test_reject_address_derived_names_defaults_off_for_other_providers(self) -> None:
        """Only LOC opts in - other providers' relevance ranking may handle a
        street address in the query fine (e.g. a phrase-matching search)."""
        self.assertFalse(_BareGateway.reject_address_derived_names)


class InternetArchiveMediaProviderRelevanceFlagsTests(SimpleTestCase):
    """Regression guard: Internet Archive has the same word-independent-OR
    relevance ranking symptom as LOC (a generic street-type word like "Road"
    coincidentally matches unrelated nationwide items), fixed the same way.

    Unlike the deleted direct gateway, REData now builds the actual upstream
    query (including any phrase-quoting archive.org's parser needs) - this
    provider just passes a clean, unquoted name + locality as ``q``."""

    def test_include_address_is_disabled(self) -> None:
        self.assertFalse(InternetArchiveMediaProvider.include_address)

    def test_reject_address_derived_names_is_enabled(self) -> None:
        self.assertTrue(InternetArchiveMediaProvider.reject_address_derived_names)

    def test_search_with_country_is_disabled(self) -> None:
        self.assertFalse(InternetArchiveMediaProvider.search_with_country)

    def test_name_and_locality_are_not_quoted(self) -> None:
        """Quoting is now REData's job (per its own ``query_styles`` per
        provider) - double-quoting here would be quoting REData's own quoting."""
        self.assertFalse(InternetArchiveMediaProvider.quote_name)
        self.assertFalse(InternetArchiveMediaProvider.quote_locality)

    def test_address_is_actually_omitted_from_the_query(self) -> None:
        """The exact reported scenario: name "Summit Road" pulled in unrelated
        nationwide results once the street address was included."""
        loc = _location(street_number="1000", route="I-75 Nb Expy", locality="Cincinnati", administrative_area_level_1="OH", official_name="Summit Road")
        pin = _pin(loc)
        terms = MediaPanelSource.search_terms(pin, InternetArchiveMediaProvider())
        self.assertEqual(terms, ["Summit Road Cincinnati OH"])


class SmithsonianMediaProviderRelevanceFlagsTests(SimpleTestCase):
    """Regression guard: Smithsonian returned irrelevant nationwide results
    for the same word-independent-OR relevance ranking reason as LOC/Internet
    Archive, compounded by an unquoted "United States" contributing noise as
    its own free-standing term across a ~19M-object US federal collection."""

    def test_reject_address_derived_names_is_enabled(self) -> None:
        self.assertTrue(SmithsonianMediaProvider.reject_address_derived_names)

    def test_include_address_is_disabled(self) -> None:
        self.assertFalse(SmithsonianMediaProvider.include_address)

    def test_search_with_country_is_disabled(self) -> None:
        self.assertFalse(SmithsonianMediaProvider.search_with_country)

    def test_name_and_locality_are_not_quoted(self) -> None:
        """Smithsonian's Solr-family parser needs quotes, but REData applies
        them server-side now (per its own ``query_styles``) - quoting here too
        would double-apply it on top of REData's."""
        self.assertFalse(SmithsonianMediaProvider.quote_name)
        self.assertFalse(SmithsonianMediaProvider.quote_locality)

    def test_query_is_a_clean_name_and_locality_without_address_or_country(self) -> None:
        loc = _location(street_number="1000", route="I-75 Nb Expy", locality="Cincinnati", administrative_area_level_1="OH", official_name="Summit Road")
        pin = _pin(loc)
        terms = MediaPanelSource.search_terms(pin, SmithsonianMediaProvider())
        self.assertEqual(terms, ["Summit Road Cincinnati OH"])
