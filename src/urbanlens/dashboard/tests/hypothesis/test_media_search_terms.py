"""Tests for MediaPanelSource.search_terms() and the LOC gateway's relevance flags.

Covers the fix for LOC returning irrelevant nationwide results for a pin with
no real landmark name (just its street address as a fallback "name") - see
services.apis.assets.loc.LOCJsonGateway and
services.locations.naming.is_address_derived_name.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from urbanlens.core.tests.testcase import SimpleTestCase
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.services.apis.assets.base import MediaItem, MediaProvider
from urbanlens.dashboard.services.apis.assets.internet_archive import InternetArchiveGateway
from urbanlens.dashboard.services.apis.assets.loc import LOCJsonGateway
from urbanlens.dashboard.services.apis.assets.smithsonian import SmithsonianGateway
from urbanlens.dashboard.services.external_data import MediaPanelSource

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


class LOCJsonGatewayRelevanceFlagsTests(SimpleTestCase):
    """Regression guard for LOC's specific configuration."""

    def test_reject_address_derived_names_is_enabled(self) -> None:
        self.assertTrue(LOCJsonGateway.reject_address_derived_names)

    def test_include_address_is_disabled(self) -> None:
        self.assertFalse(LOCJsonGateway.include_address)

    def test_reject_address_derived_names_defaults_off_for_other_providers(self) -> None:
        """Only LOC opts in - other providers' relevance ranking may handle a
        street address in the query fine (e.g. a phrase-matching search)."""
        self.assertFalse(_BareGateway.reject_address_derived_names)


class InternetArchiveGatewayRelevanceFlagsTests(SimpleTestCase):
    """Regression guard: Internet Archive has the same word-independent-OR
    relevance ranking symptom as LOC (a generic street-type word like "Road"
    coincidentally matches unrelated nationwide items), fixed the same way."""

    def test_include_address_is_disabled(self) -> None:
        self.assertFalse(InternetArchiveGateway.include_address)

    def test_address_is_actually_omitted_from_the_query(self) -> None:
        """The exact reported scenario: name "Summit Road" pulled in unrelated
        nationwide results once the street address was included unquoted."""
        loc = _location(street_number="1000", route="I-75 Nb Expy", locality="Cincinnati", administrative_area_level_1="OH", official_name="Summit Road")
        pin = _pin(loc)
        terms = MediaPanelSource.search_terms(pin, InternetArchiveGateway())
        self.assertNotEqual(terms, [])
        self.assertIn("Summit Road", terms[0])
        self.assertNotIn("I-75", terms[0])


class SmithsonianGatewayRelevanceFlagsTests(SimpleTestCase):
    """Regression guard: Smithsonian returned irrelevant nationwide results
    for the same word-independent-OR relevance ranking reason as LOC/Internet
    Archive, compounded by an unquoted "United States" contributing noise as
    its own free-standing term across a ~19M-object US federal collection."""

    def test_reject_address_derived_names_is_enabled(self) -> None:
        self.assertTrue(SmithsonianGateway.reject_address_derived_names)

    def test_include_address_is_disabled(self) -> None:
        self.assertFalse(SmithsonianGateway.include_address)

    def test_search_with_country_is_disabled(self) -> None:
        self.assertFalse(SmithsonianGateway.search_with_country)

    def test_quote_name_is_enabled(self) -> None:
        self.assertTrue(SmithsonianGateway.quote_name)

    def test_quote_locality_is_enabled(self) -> None:
        self.assertTrue(SmithsonianGateway.quote_locality)

    def test_query_is_quoted_name_and_locality_without_address_or_country(self) -> None:
        loc = _location(street_number="1000", route="I-75 Nb Expy", locality="Cincinnati", administrative_area_level_1="OH", official_name="Summit Road")
        pin = _pin(loc)
        terms = MediaPanelSource.search_terms(pin, SmithsonianGateway(api_key="test-key"))
        self.assertEqual(terms, ['"Summit Road" "Cincinnati OH"'])
