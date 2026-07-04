"""Tests for LoopNetGateway - covers listing de-duplication, duplicate-link
labeling, and property-record page scraping in the web-search fallback path.

All HTTP calls are mocked so no real network access occurs.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from hypothesis import given, settings, strategies as st

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.services.apis.real_estate.loopnet import (
    LoopNetGateway,
    _find_property_listing,
    _group_listings,
    _listing_group_key,
    _listing_label,
    _listing_rank,
    _parse_property_page,
    _select_primary,
)

_hyp = settings(max_examples=50, deadline=None)


def _listing(url: str, snippet: str = "", title: str = "") -> dict:
    """Build a minimal listing dict as produced by the web-search fallback."""
    return {"title": title, "url": url, "snippet": snippet}


def _gateway() -> LoopNetGateway:
    """Return a LoopNetGateway with a stub session (no real HTTP)."""
    session = MagicMock()
    return LoopNetGateway(session=session)


_PROPERTY_PAGE_HTML = """
<section id="property-details-info" class="highlights include-in-page">
    <section id="detailsInf" class="highlights include-in-page">
        <div class="flex">
            <div class="flex-cell-border" data-automation-id="Address">
                <div class="column-06 column-tiny-06 no-padding-left assessment-key"><label>Address</label></div>
                <div class="column-06 column-tiny-06 assessment-value">3500 Montgomery Rd</div>
            </div>
            <div class="flex-cell-border" data-automation-id="ApnComps">
                <div class="column-06 column-tiny-06 no-padding-left assessment-key"><label>APN/Parcel ID</label></div>
                <div class="column-06 column-tiny-06 assessment-value">058-0001-0002</div>
            </div>
            <div class="flex-cell-border" data-automation-id="LotAcresTotal">
                <div class="column-06 column-tiny-06 no-padding-left assessment-key"><label>Lot Size</label></div>
                <div class="column-06 column-tiny-06 assessment-value">2.36 AC</div>
            </div>
        </div>
    </section>
</section>
"""


# ---------------------------------------------------------------------------
# _listing_group_key
# ---------------------------------------------------------------------------

class ListingGroupKeyTests(TestCase):
    """_listing_group_key groups /Listing/ and /property/ URLs for the same address."""

    def test_listing_and_property_urls_for_same_address_share_a_key(self):
        listing_key = _listing_group_key("https://www.loopnet.com/Listing/3500-Montgomery-Rd-Cincinnati-OH/38555377/")
        property_key = _listing_group_key(
            "https://www.loopnet.com/property/3500-montgomery-rd-cincinnati-oh-45207/39061-0580001000200/",
        )
        self.assertEqual(listing_key, property_key)

    def test_bare_domain_and_www_urls_share_a_key(self):
        www_key = _listing_group_key("https://www.loopnet.com/Listing/3500-Montgomery-Rd-Cincinnati-OH/38555377/")
        bare_key = _listing_group_key("https://loopnet.com/Listing/3500-Montgomery-Rd-Cincinnati-OH/23839564")
        self.assertEqual(www_key, bare_key)

    def test_different_addresses_have_different_keys(self):
        key_a = _listing_group_key("https://www.loopnet.com/Listing/100-Main-St-Cincinnati-OH/1/")
        key_b = _listing_group_key("https://www.loopnet.com/Listing/200-Main-St-Cincinnati-OH/2/")
        self.assertNotEqual(key_a, key_b)

    def test_key_is_case_insensitive(self):
        lower = _listing_group_key("https://www.loopnet.com/Listing/3500-montgomery-rd-cincinnati-oh/1/")
        upper = _listing_group_key("https://www.loopnet.com/Listing/3500-MONTGOMERY-RD-CINCINNATI-OH/1/")
        self.assertEqual(lower, upper)

    def test_unparseable_url_falls_back_to_lowercased_url(self):
        url = "https://example.com/Not-A-LoopNet-Url"
        self.assertEqual(_listing_group_key(url), url.lower())


# ---------------------------------------------------------------------------
# _listing_rank / _listing_label
# ---------------------------------------------------------------------------

class ListingRankTests(TestCase):
    """_listing_rank scores active listings best, then property pages, then expired listings."""

    def test_active_listing_ranks_best(self):
        listing = _listing("https://www.loopnet.com/Listing/x/1/", snippet="currently available.")
        self.assertEqual(_listing_rank(listing), 0)

    def test_property_page_ranks_middle(self):
        listing = _listing("https://www.loopnet.com/property/x/1/", snippet="contains information about the property.")
        self.assertEqual(_listing_rank(listing), 1)

    def test_expired_listing_ranks_worst(self):
        listing = _listing("https://www.loopnet.com/Listing/x/1/", snippet="is no longer being advertised on LoopNet.com.")
        self.assertEqual(_listing_rank(listing), 2)

    def test_expired_property_page_ranks_worst(self):
        listing = _listing("https://www.loopnet.com/property/x/1/", snippet="is no longer being advertised on LoopNet.com.")
        self.assertEqual(_listing_rank(listing), 2)


class ListingLabelTests(TestCase):
    """_listing_label gives each secondary link a short, semantic name."""

    def test_expired_listing_labeled_archived(self):
        listing = _listing("https://www.loopnet.com/Listing/x/1/", snippet="is no longer being advertised on LoopNet.com.")
        self.assertEqual(_listing_label(listing), "Archived listing")

    def test_property_page_labeled_parcel_record(self):
        listing = _listing("https://www.loopnet.com/property/x/1/", snippet="contains information about the property.")
        self.assertEqual(_listing_label(listing), "Parcel record")

    def test_other_active_listing_labeled_additional(self):
        listing = _listing("https://loopnet.com/Listing/x/2/", snippet="currently available.")
        self.assertEqual(_listing_label(listing), "Additional listing")


# ---------------------------------------------------------------------------
# _group_listings / _select_primary / _find_property_listing
# ---------------------------------------------------------------------------

class GroupListingsTests(TestCase):
    """_group_listings clusters same-property URLs while preserving order."""

    def test_four_hits_for_same_property_form_one_group(self):
        listings = [
            _listing("https://www.loopnet.com/Listing/3500-Montgomery-Rd-Cincinnati-OH/38555377/", title="A"),
            _listing("https://www.loopnet.com/property/3500-montgomery-rd-cincinnati-oh-45207/39061-0580001000200/", title="B"),
            _listing("https://www.loopnet.com/Listing/3500-Montgomery-Rd-Cincinnati-OH/11085087/", title="C"),
            _listing("https://loopnet.com/Listing/3500-Montgomery-Rd-Cincinnati-OH/23839564", title="D"),
        ]
        groups = _group_listings(listings)
        self.assertEqual(len(groups), 1)
        self.assertEqual(len(next(iter(groups.values()))), 4)

    def test_distinct_addresses_form_distinct_groups(self):
        listings = [
            _listing("https://www.loopnet.com/Listing/100-Main-St-Cincinnati-OH/1/", title="X"),
            _listing("https://www.loopnet.com/Listing/200-Main-St-Cincinnati-OH/2/", title="Y"),
        ]
        groups = _group_listings(listings)
        self.assertEqual(len(groups), 2)

    def test_group_order_matches_first_seen_order(self):
        listings = [
            _listing("https://www.loopnet.com/Listing/300-Elm-St/1/", title="first"),
            _listing("https://www.loopnet.com/Listing/100-Main-St/2/", title="second"),
        ]
        groups = _group_listings(listings)
        titles = [group[0]["title"] for group in groups.values()]
        self.assertEqual(titles, ["first", "second"])

    @given(st.lists(st.sampled_from(["100-Main-St", "200-Elm-St", "300-Oak-Ave"]), min_size=0, max_size=15))
    @_hyp
    def test_group_count_never_exceeds_distinct_slug_count(self, slugs):
        listings = [_listing(f"https://www.loopnet.com/Listing/{slug}/{i}/") for i, slug in enumerate(slugs)]
        groups = _group_listings(listings)
        self.assertLessEqual(len(groups), len(set(slugs)))

    @given(st.lists(st.sampled_from(["100-Main-St", "200-Elm-St", "300-Oak-Ave"]), min_size=1, max_size=15))
    @_hyp
    def test_every_listing_appears_in_exactly_one_group(self, slugs):
        listings = [_listing(f"https://www.loopnet.com/Listing/{slug}/{i}/") for i, slug in enumerate(slugs)]
        groups = _group_listings(listings)
        total_grouped = sum(len(group) for group in groups.values())
        self.assertEqual(total_grouped, len(listings))


class SelectPrimaryTests(TestCase):
    """_select_primary picks the most useful listing, ties going to the first-seen."""

    def test_prefers_active_listing_over_property_page(self):
        group = [
            _listing("https://www.loopnet.com/property/100-Main-St/1/", "property info.", "property-page"),
            _listing("https://www.loopnet.com/Listing/100-Main-St/2/", "currently available.", "active-listing"),
        ]
        self.assertEqual(_select_primary(group)["title"], "active-listing")

    def test_prefers_property_page_over_expired_listing(self):
        group = [
            _listing("https://www.loopnet.com/Listing/100-Main-St/1/", "is no longer being advertised on LoopNet.com.", "expired"),
            _listing("https://www.loopnet.com/property/100-main-st/1/", "property info.", "property-page"),
        ]
        self.assertEqual(_select_primary(group)["title"], "property-page")

    def test_ties_go_to_first_seen(self):
        group = [
            _listing("https://www.loopnet.com/Listing/100-Main-St/1/", "currently available.", "first"),
            _listing("https://loopnet.com/Listing/100-Main-St/2/", "currently available.", "second"),
        ]
        self.assertEqual(_select_primary(group)["title"], "first")


class FindPropertyListingTests(TestCase):
    """_find_property_listing returns the group's /property/ entry, if any."""

    def test_returns_property_entry_when_present(self):
        group = [
            _listing("https://www.loopnet.com/Listing/100-Main-St/1/", title="listing"),
            _listing("https://www.loopnet.com/property/100-Main-St/1/", title="property"),
        ]
        self.assertEqual(_find_property_listing(group)["title"], "property")

    def test_returns_none_when_absent(self):
        group = [_listing("https://www.loopnet.com/Listing/100-Main-St/1/", title="listing")]
        self.assertIsNone(_find_property_listing(group))


# ---------------------------------------------------------------------------
# _parse_property_page
# ---------------------------------------------------------------------------

class ParsePropertyPageTests(TestCase):
    """_parse_property_page extracts the assessment key/value grid."""

    def test_extracts_expected_fields(self):
        details = _parse_property_page(_PROPERTY_PAGE_HTML)
        self.assertEqual(details["Address"], "3500 Montgomery Rd")
        self.assertEqual(details["APN/Parcel ID"], "058-0001-0002")
        self.assertEqual(details["Lot Size"], "2.36 AC")

    def test_returns_none_for_html_with_no_assessment_data(self):
        self.assertIsNone(_parse_property_page("<html><body><p>No data here</p></body></html>"))

    def test_returns_none_for_empty_string(self):
        self.assertIsNone(_parse_property_page(""))


# ---------------------------------------------------------------------------
# LoopNetGateway._dedupe_and_enrich
# ---------------------------------------------------------------------------

class DedupeAndEnrichTests(TestCase):
    """_dedupe_and_enrich collapses duplicate URLs and attaches scraped property data."""

    def setUp(self):
        self.gw = _gateway()

    def test_four_hits_collapse_to_one_primary_listing(self):
        listings = [
            _listing("https://www.loopnet.com/Listing/3500-Montgomery-Rd-Cincinnati-OH/38555377/", "currently available.", "A"),
            _listing(
                "https://www.loopnet.com/property/3500-montgomery-rd-cincinnati-oh-45207/39061-0580001000200/",
                "contains information about the property.",
                "B",
            ),
            _listing("https://www.loopnet.com/Listing/3500-Montgomery-Rd-Cincinnati-OH/11085087/", "is no longer being advertised on LoopNet.com.", "C"),
            _listing("https://loopnet.com/Listing/3500-Montgomery-Rd-Cincinnati-OH/23839564", "currently available.", "D"),
        ]
        with patch.object(LoopNetGateway, "_fetch_property_details", return_value=None):
            primary_listings, duplicate_links = self.gw._dedupe_and_enrich(listings)
        self.assertEqual(len(primary_listings), 1)
        self.assertEqual(primary_listings[0]["title"], "A")
        self.assertEqual(len(duplicate_links), 3)

    def test_duplicate_links_carry_semantic_labels(self):
        listings = [
            _listing("https://www.loopnet.com/Listing/100-Main-St/1/", "currently available.", "active"),
            _listing("https://www.loopnet.com/property/100-main-st/1/", "property info.", "parcel"),
            _listing("https://www.loopnet.com/Listing/100-Main-St/2/", "is no longer being advertised on LoopNet.com.", "expired"),
        ]
        with patch.object(LoopNetGateway, "_fetch_property_details", return_value=None):
            _, duplicate_links = self.gw._dedupe_and_enrich(listings)
        labels = {link["label"] for link in duplicate_links}
        self.assertEqual(labels, {"Parcel record", "Archived listing"})

    def test_fetches_property_details_for_property_page_in_group(self):
        listings = [
            _listing("https://www.loopnet.com/Listing/100-Main-St/1/", "currently available.", "active"),
            _listing("https://www.loopnet.com/property/100-main-st/1/", "property info.", "parcel"),
        ]
        with patch.object(LoopNetGateway, "_fetch_property_details", return_value={"APN/Parcel ID": "1-2-3"}) as mock_fetch:
            primary_listings, _ = self.gw._dedupe_and_enrich(listings)
        mock_fetch.assert_called_once_with("https://www.loopnet.com/property/100-main-st/1/")
        self.assertEqual(primary_listings[0]["property_details"], {"APN/Parcel ID": "1-2-3"})

    def test_no_property_details_key_when_no_property_page_in_group(self):
        listings = [_listing("https://www.loopnet.com/Listing/100-Main-St/1/", "currently available.", "active")]
        with patch.object(LoopNetGateway, "_fetch_property_details") as mock_fetch:
            primary_listings, _ = self.gw._dedupe_and_enrich(listings)
        mock_fetch.assert_not_called()
        self.assertNotIn("property_details", primary_listings[0])

    def test_distinct_properties_each_get_their_own_primary_and_no_cross_links(self):
        listings = [
            _listing("https://www.loopnet.com/Listing/100-Main-St/1/", title="X"),
            _listing("https://www.loopnet.com/Listing/200-Elm-St/2/", title="Y"),
        ]
        with patch.object(LoopNetGateway, "_fetch_property_details", return_value=None):
            primary_listings, duplicate_links = self.gw._dedupe_and_enrich(listings)
        self.assertEqual({p["title"] for p in primary_listings}, {"X", "Y"})
        self.assertEqual(duplicate_links, [])


# ---------------------------------------------------------------------------
# LoopNetGateway._fetch_property_details
# ---------------------------------------------------------------------------

class FetchPropertyDetailsTests(TestCase):
    """_fetch_property_details fetches and parses a property-record page."""

    def setUp(self):
        self.gw = _gateway()

    def test_returns_parsed_details_on_success(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = _PROPERTY_PAGE_HTML
        mock_resp.raise_for_status.return_value = None
        self.gw.session.get.return_value = mock_resp

        details = self.gw._fetch_property_details("https://www.loopnet.com/property/x/1/")

        self.assertEqual(details["APN/Parcel ID"], "058-0001-0002")

    def test_returns_none_when_blocked(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 403
        self.gw.session.get.return_value = mock_resp

        details = self.gw._fetch_property_details("https://www.loopnet.com/property/x/1/")

        self.assertIsNone(details)

    def test_returns_none_on_request_exception(self):
        import requests.exceptions

        self.gw.session.get.side_effect = requests.exceptions.ConnectionError("boom")

        details = self.gw._fetch_property_details("https://www.loopnet.com/property/x/1/")

        self.assertIsNone(details)
