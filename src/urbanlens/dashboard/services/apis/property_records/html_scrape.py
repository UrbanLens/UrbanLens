"""Shared HTML scrape engine for Tier 2 (vendor) and Tier 3 (bespoke) property lookups.

Both tiers boil down to the same two steps once a jurisdiction is
configured: build one HTTP request from a bounded, allowlisted recipe, then
pull labeled fields out of whatever HTML comes back. This module owns both,
so a Tier 2 vendor template (``vendor_templates.py``) and a Tier 3
per-jurisdiction ``PropertyJurisdiction.scrape_recipe`` are just two sources
of the same :class:`ScrapeRecipe` shape, executed identically.

Security discipline (per ``docs/property-records-plan.md``'s Tier 3
compliance note): a recipe can only ever place a value from
:class:`SearchField` - one of a small, fixed set of data UrbanLens already
knows about a property (its situs address, an APN if already known) - into
the request. There is no way to construct a recipe that sends arbitrary
text, and nothing here ever executes recipe-supplied code or selectors
against a live DOM (no browser, no JS) - it's a plain HTTP GET/POST plus
regex/lxml-based text extraction on the response body. An AI-assisted
recipe author (``discovery.py``) is bound by the same dataclass and the same
:class:`SearchField` allowlist, so it structurally cannot propose anything
riskier than "put the address in this query param."
"""

from __future__ import annotations

from dataclasses import dataclass, field
import logging
from typing import Any, ClassVar

from requests.exceptions import RequestException

from urbanlens.dashboard.services.apis.property_records.meta import SCRAPE_USER_AGENT
from urbanlens.dashboard.services.gateway import Gateway

logger = logging.getLogger(__name__)

_REQUEST_TIMEOUT_SECONDS = 20
_MAX_RESPONSE_BYTES = 3 * 1024 * 1024


class SearchField:
    """The only values a :class:`ScrapeRecipe` may ever place into a request.

    Deliberately not user/AI-extensible - adding a new one is a code change,
    not data. Every value here is something UrbanLens already knows about
    the property being looked up (never anything from an untrusted source).
    """

    SITUS_ADDRESS: ClassVar[str] = "situs_address"
    APN: ClassVar[str] = "apn"

    ALL: ClassVar[frozenset[str]] = frozenset({SITUS_ADDRESS, APN})


@dataclass(frozen=True, slots=True)
class ScrapeRecipe:
    """A bounded description of one search request against a Tier 2/3 site.

    Attributes:
        base_url: The fixed target URL (a vendor template's own endpoint, or
            a jurisdiction's ``scrape_recipe`` value) - never built from
            request-time data.
        method: ``"GET"`` or ``"POST"``.
        search_field: Which known-safe value (see :class:`SearchField`) this
            recipe searches by.
        param_name: The query parameter (GET) or form field (POST) name the
            search value is placed into.
        extra_params: Fixed, non-substitutable extra query/form parameters
            (e.g. a vendor platform's per-county ``AppID``/``LayerID``) -
            these come from the recipe's own configuration, never from an
            untrusted source either.
    """

    base_url: str
    search_field: str
    param_name: str
    method: str = "GET"
    extra_params: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.search_field not in SearchField.ALL:
            raise ValueError(f"Unknown search_field {self.search_field!r} - must be one of {sorted(SearchField.ALL)}")
        if self.method not in ("GET", "POST"):
            raise ValueError(f"Unsupported method {self.method!r} - must be GET or POST")


def recipe_from_dict(data: dict[str, Any]) -> ScrapeRecipe | None:
    """Parse a ``PropertyJurisdiction.scrape_recipe`` JSON payload into a :class:`ScrapeRecipe`.

    Treats the stored JSON as untrusted (it may have been written by an
    AI-assisted discovery pass) - any shape that doesn't cleanly build a
    valid recipe returns None rather than raising.

    Args:
        data: The raw ``scrape_recipe`` dict.

    Returns:
        A validated recipe, or None if the data is empty/malformed.
    """
    if not data or not isinstance(data, dict):
        return None
    try:
        extra = data.get("extra_params") or {}
        if not isinstance(extra, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in extra.items()):
            extra = {}
        return ScrapeRecipe(
            base_url=str(data["base_url"]),
            search_field=str(data["search_field"]),
            param_name=str(data["param_name"]),
            method=str(data.get("method", "GET")).upper(),
            extra_params=dict(extra),
        )
    except (KeyError, ValueError, TypeError):
        return None


def recipe_to_dict(recipe: ScrapeRecipe) -> dict[str, Any]:
    """Serialize a :class:`ScrapeRecipe` back to JSON for ``PropertyJurisdiction.scrape_recipe``."""
    return {
        "base_url": recipe.base_url,
        "search_field": recipe.search_field,
        "param_name": recipe.param_name,
        "method": recipe.method,
        "extra_params": dict(recipe.extra_params),
    }


class _ScrapeGateway(Gateway):
    """Thin Gateway wrapper so Tier 2/3 fetches get the standard rate-limit/call-log treatment."""

    service_key: ClassVar[str] = "property_records_scrape"  # pyright: ignore[reportIncompatibleVariableOverride]
    paid_service: ClassVar[bool] = False


def execute_scrape_recipe(recipe: ScrapeRecipe, *, situs_address: str = "", apn: str = "") -> dict[str, str] | None:
    """Run one recipe's search request and extract whatever labeled fields it finds.

    Args:
        recipe: The recipe to execute.
        situs_address: The property's known situs address, used only if
            ``recipe.search_field == SearchField.SITUS_ADDRESS``.
        apn: The property's known APN, used only if
            ``recipe.search_field == SearchField.APN``.

    Returns:
        A raw label -> value dict extracted from the response HTML (run this
        through ``field_mapping.map_fields`` to normalize), or None when the
        search value is missing, the request fails, or nothing extractable was found.
    """
    value = situs_address if recipe.search_field == SearchField.SITUS_ADDRESS else apn
    if not value:
        return None

    params = {**recipe.extra_params, recipe.param_name: value}

    gateway = _ScrapeGateway()
    headers = {"User-Agent": SCRAPE_USER_AGENT}
    try:
        import requests.exceptions

        if recipe.method == "POST":
            response = gateway.session.post(recipe.base_url, data=params, headers=headers, timeout=_REQUEST_TIMEOUT_SECONDS, stream=True)
        else:
            response = gateway.session.get(recipe.base_url, params=params, headers=headers, timeout=_REQUEST_TIMEOUT_SECONDS, stream=True)
        response.raise_for_status()
        body = b""
        for chunk in response.iter_content(chunk_size=65536):
            body += chunk
            if len(body) > _MAX_RESPONSE_BYTES:
                break
    except RequestException:
        logger.debug("Tier 2/3 property-records scrape request failed for %s", recipe.base_url, exc_info=True)
        return None

    html = body.decode(response.encoding or "utf-8", errors="replace")
    return extract_label_value_pairs(html) or None


def extract_label_value_pairs(html: str) -> dict[str, str]:
    """Pull ``label -> value`` pairs out of an assessor-style HTML page.

    Tries, in order, the DOM shapes most common on government/vendor
    property-record pages: two-cell table rows, definition lists, and
    generically-classed "label"/"value" element pairs (the same shape
    ``services.apis.real_estate.loopnet``'s existing scraper already handles
    for one specific site, generalized here to any class name containing
    "label"/"value"). Never executes any script or renders the page -
    plain server-returned HTML only.

    Args:
        html: Raw HTML of a search-result or detail page.

    Returns:
        Mapping of cleaned label text to cleaned value text - empty if
        nothing recognizable was found or the HTML didn't parse.
    """
    try:
        from defusedxml.lxml import fromstring as defused_fromstring

        # HTMLParser is config only; parsing uses defused_fromstring
        from lxml.etree import HTMLParser, LxmlError  # nosec B410
    except ImportError:
        return {}

    try:
        tree = defused_fromstring(html.encode(), parser=HTMLParser())
    except (LxmlError, AttributeError):
        # defusedxml.lxml.fromstring raises AttributeError (not LxmlError)
        # when the underlying lxml parse yields no root element.
        return {}
    if tree is None:
        return {}

    pairs: dict[str, str] = {}
    pairs.update(_extract_table_rows(tree))
    pairs.update(_extract_definition_lists(tree))
    pairs.update(_extract_labeled_class_pairs(tree))
    return pairs


def _text(element: Any) -> str:
    return " ".join("".join(element.itertext()).split())


def _extract_table_rows(tree: Any) -> dict[str, str]:
    """``<tr><td>Label</td><td>Value</td></tr>`` (or ``<th>``) two-cell rows."""
    pairs: dict[str, str] = {}
    for row in tree.xpath(".//tr"):
        cells = row.xpath("./td | ./th")
        if len(cells) != 2:
            continue
        label = _text(cells[0]).rstrip(":").strip()
        value = _text(cells[1]).strip()
        if label and value and len(label) <= 80:
            pairs[label] = value
    return pairs


def _extract_definition_lists(tree: Any) -> dict[str, str]:
    """``<dt>Label</dt><dd>Value</dd>`` pairs, matched by document order within each ``<dl>``."""
    pairs: dict[str, str] = {}
    for dl in tree.xpath(".//dl"):
        terms = dl.xpath("./dt")
        definitions = dl.xpath("./dd")
        for label_el, value_el in zip(terms, definitions, strict=False):
            label = _text(label_el).rstrip(":").strip()
            value = _text(value_el).strip()
            if label and value and len(label) <= 80:
                pairs[label] = value
    return pairs


def _extract_labeled_class_pairs(tree: Any) -> dict[str, str]:
    """Elements whose class contains "label"/"key" paired with a following "value" sibling.

    Generalizes the ``[data-automation-id]`` label/value grid pattern
    ``services.apis.real_estate.loopnet._parse_property_page`` already
    handles for one specific site - here matched purely by class-name
    substring so it applies to any similarly-shaped vendor markup.
    """
    pairs: dict[str, str] = {}
    label_class_xpath = ".//*[contains(concat(' ', normalize-space(@class), ' '), ' label ') or contains(concat(' ', normalize-space(@class), ' '), ' key ')]"
    for label_el in tree.xpath(label_class_xpath):
        parent = label_el.getparent()
        if parent is None:
            continue
        value_el = parent.xpath(".//*[contains(concat(' ', normalize-space(@class), ' '), ' value ')]")
        if not value_el:
            continue
        label = _text(label_el).rstrip(":").strip()
        value = _text(value_el[0]).strip()
        if label and value and len(label) <= 80:
            pairs[label] = value
    return pairs
