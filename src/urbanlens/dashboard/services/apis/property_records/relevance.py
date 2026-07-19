"""Pure relevance heuristics for property-record endpoint discovery.

Everything in this module answers one question - "does this candidate look
like a county's real, comprehensive parcel/tax dataset, and how does it
compare to its rivals?" - with plain data in and plain data out. No HTTP, no
ORM, no side effects: ``discovery.py`` owns all the live probing and calls in
here for every accept/reject/ordering decision, so each rule exists exactly
once no matter how many fetch paths use it.

Every threshold and pattern below was calibrated against a live failure on a
real county's data, not invented defensively. The condensed rationale is
kept beside each rule; the full war stories (which county, which dataset,
what went wrong) live in ``docs/NOTES.md`` under "Property-records discovery
heuristics".

The three-stage acceptance gate (see ``discovery._validate_endpoint`` /
``discovery._refine_arcgis_url`` for how it interleaves with live fetches):

1. :func:`url_is_disqualified` - free, pre-fetch; kills obviously-wrong URLs
   before any request is spent on them.
2. :func:`layer_is_acceptable` - post-schema-fetch; name + field-shape checks.
3. :func:`count_is_sufficient` - post-count-probe; "is there really a whole
   county behind this?"
"""

from __future__ import annotations

from functools import cache
import re
from typing import TYPE_CHECKING

from urbanlens.dashboard.services.apis.property_records.field_mapping import _HEURISTIC_CANDIDATES, _normalize_key

if TYPE_CHECKING:
    from collections.abc import Sequence

#: Names suggesting parcel/assessment data. Excludes bare "land": real
#: statewide agricultural land-use/crop layers matched on it ("landuse")
#: while holding zero ownership/tax fields; "land records"/"land ownership"
#: (genuine naming conventions) still match via the tighter phrases.
PARCEL_LAYER_NAME_RE = re.compile(r"parcel|cadastr|tax|assess|propert|land\s*records?|land\s*ownership", re.IGNORECASE)

#: Titles (separator/case-insensitive) that unambiguously mean "the standard
#: parcel layer". Only these may pass on name alone with no corroborating
#: field - looser matches ("Mineral_Parcels", "..._Cadastral") have collided
#: with non-ownership datasets in both directions live.
CANONICAL_PARCEL_TITLES = frozenset({"parcels", "parcel", "tax parcels", "property parcels", "real property parcels", "ownership parcels"})

#: Titles flagging a superseded snapshot ("Previous Parcels") - deprioritized
#: behind an equally-good unflagged rival ("Current Parcels"), never rejected:
#: a whole-county snapshot from last year still beats nothing.
STALE_TITLE_MARKERS_RE = re.compile(r"\bprevious(ly)?\b|\bhistoric(al)?\b|\barchive[d]?\b|\bdeprecated\b|\bformer(ly)?\b|\bold\b", re.IGNORECASE)

TITLE_SEPARATOR_RE = re.compile(r"[\s_-]+")

#: Slugs marking a narrow, non-comprehensive slice of parcels (delinquency
#: trackers, easement/agricultural-program registries, environmental-
#: litigation trackers) - rejected outright, unlike staleness: these cover a
#: tiny unrepresentative fraction of real properties, which is worse than
#: finding nothing. ``consent.?decree`` added live: a Kent County, MI search
#: accepted a 3,793-row "Parcel Status from February 2020 Consent Decree"
#: layer (a groundwater-contamination litigation tracker, real Kent County
#: parcels but a sliver of its ~220k total) - its own title says "Parcel",
#: so :func:`portal_item_is_plausible` correctly let it through; only a
#: narrow-subset marker could catch it.
NON_COMPREHENSIVE_SUBSET_RE = re.compile(r"delinquen|forfeit|tax.?sale|foreclos|easement|agricultural|conservation|consent.?decree", re.IGNORECASE)

#: Non-production markers, word-bounded and matched against separator-
#: normalized text (an underscore is a word character, so ``\btest\b`` never
#: fires inside ``..._Test/`` without normalizing first).
NON_PRODUCTION_MARKERS_RE = re.compile(r"\btest\b|\bstaging\b|\bdemo\b|\bsample\b", re.IGNORECASE)

#: Every raw field name ``field_mapping`` can already map, normalized once -
#: reused so "looks like parcel data" and "can actually be mapped" stay the
#: same definition.
PARCEL_FIELD_CANDIDATES: frozenset[str] = frozenset(_normalize_key(candidate) for candidates in _HEURISTIC_CANDIDATES.values() for candidate in candidates)

#: Corroborating-field floor when the name alone doesn't say "parcels".
MIN_PARCEL_FIELD_MATCHES = 2

#: Minimum features/rows for a dataset to plausibly be a whole county.
#: Calibrated upward twice from live false accepts (a 1-feature extract, a
#: 121-row stray upload, a 1,153-row program registry, a 588-row highway
#: project) - well below any real county's parcel count, well above every
#: observed partial.
MIN_PARCEL_FEATURE_COUNT = 2000

#: "<name> County" phrase finder, applied to separator-normalized titles.
COUNTY_NAME_IN_TITLE_RE = re.compile(r"\b([a-z]+(?:\s[a-z]+)?)\scounty\b", re.IGNORECASE)


def title_rank(title: str) -> tuple[int, bool]:
    """Sort key ordering multiple plausible candidates for the same county.

    First element: 0 for an unambiguous canonical parcel title, 1 for a
    looser parcel-ish match, 2 for neither. Second: whether the title flags
    itself as a superseded snapshot (sorted after same-tier rivals). Never
    used to reject outright - a small county's only real layer is often
    named loosely, and a stale snapshot beats nothing when it's all there is.

    Args:
        title: The candidate's display title or layer name.

    Returns:
        ``(specificity_tier, is_stale)`` - lower sorts first.
    """
    normalized = TITLE_SEPARATOR_RE.sub(" ", title).strip().casefold()
    is_stale = bool(STALE_TITLE_MARKERS_RE.search(title))
    if normalized in CANONICAL_PARCEL_TITLES:
        return 0, is_stale
    if PARCEL_LAYER_NAME_RE.search(title):
        return 1, is_stale
    return 2, is_stale


def matching_field_count(fields: Sequence[object] | None) -> int:
    """How many of a candidate's own field names resemble a known parcel/tax field.

    Args:
        fields: Raw field-name strings (ArcGIS ``fields[].name``, or a
            Socrata row's keys), or None when unavailable.

    Returns:
        Count of fields whose normalized name is a known parcel/tax candidate.
    """
    if not fields:
        return 0
    return sum(1 for value in fields if isinstance(value, str) and _normalize_key(value) in PARCEL_FIELD_CANDIDATES)


def looks_like_parcel_data(name: str, fields: Sequence[object] | None) -> bool:
    """Whether a candidate's own name or field names indicate parcel/tax data.

    A name alone is only trusted when canonical; a loose name match needs at
    least one corroborating field (live counterexamples exist in both
    directions - a "..._Cadastral" coastal boundary with zero parcel fields,
    and a schools layer with a plausible field or two); with no name signal,
    :data:`MIN_PARCEL_FIELD_MATCHES` fields must corroborate.

    Args:
        name: The candidate's own name ('' when unavailable, e.g. Socrata).
        fields: Raw field-name strings, or None when unavailable.

    Returns:
        True when the evidence clears the bar described above.
    """
    field_matches = matching_field_count(fields)
    tier, _stale = title_rank(name or "")
    if tier == 0:
        return True
    if tier == 1:
        return field_matches >= 1
    return field_matches >= MIN_PARCEL_FIELD_MATCHES


def portal_item_is_plausible(title: str, snippet: str) -> bool:
    """Pre-probe rejection for an ArcGIS Portal item search result: does it look parcel-related at all?

    Built from a live-confirmed failure: an AGOL item-search for "Boone
    County Missouri parcels" surfaced (among the top handful of results) a
    one-off watershed-analysis dataset (item title ``GBFW_data_20240926``,
    snippet "Data for the Greater Bonne Femme Watershed analysis 2024") that
    happened to contain a sub-layer plainly named "Parcels" - a small,
    geographically-real-but-non-comprehensive extract of parcels within the
    study area, genuinely inside Boone County's extent (so the extent-overlap
    check didn't catch it) and just barely over :data:`MIN_PARCEL_FEATURE_COUNT`.
    AGOL's own item search is a fuzzy free-text match across title/tags/
    description, not a guarantee of relevance - unlike a canonical *leaf
    layer* name (trusted alone; a real county's actual parcel layer is
    routinely just named "Parcels" with nothing else distinguishing it), the
    *container item* surfaced by search has no such excuse: a genuine
    county open-data parcels item's own title or description essentially
    always says so somewhere. Applied before any live probe is spent on the
    item, unlike :func:`layer_is_acceptable`'s deeper, post-fetch checks.

    Args:
        title: The portal item's own title.
        snippet: The portal item's own description/snippet ('' when absent).

    Returns:
        True when the title or snippet mentions parcels/assessment data.
    """
    return bool(PARCEL_LAYER_NAME_RE.search(title) or PARCEL_LAYER_NAME_RE.search(snippet))


def url_is_disqualified(url: str) -> bool:
    """Pre-fetch rejection: the URL's own text marks it as a wrong kind of dataset.

    Catches narrow-subset slugs (:data:`NON_COMPREHENSIVE_SUBSET_RE`) and
    non-production markers (:data:`NON_PRODUCTION_MARKERS_RE`) before any
    request is spent probing the endpoint.

    Args:
        url: The candidate endpoint URL.

    Returns:
        True when the URL should be rejected without probing.
    """
    return bool(NON_COMPREHENSIVE_SUBSET_RE.search(url)) or bool(NON_PRODUCTION_MARKERS_RE.search(TITLE_SEPARATOR_RE.sub(" ", url)))


def layer_is_acceptable(name: str, fields: Sequence[object] | None) -> bool:
    """Post-schema-fetch acceptance: name/marker/field checks on one concrete layer.

    Combines three rules every fetch path must apply identically:

    * A present-but-empty schema is rejected no matter how canonical the name
      (a real county's "Tax Parcels" test layer served ``fields: null``) -
      such a layer can never produce a usable record.
    * A name carrying a narrow-subset or non-production marker is rejected
      (mirrors :func:`url_is_disqualified` for names like "Tax Delinquent
      Properties" living at an innocuous URL).
    * Otherwise, :func:`looks_like_parcel_data` decides.

    Args:
        name: The layer/resource's own name ('' when unavailable).
        fields: Raw field-name strings; empty means "schema known and empty"
            (rejected), None means "schema shape unknowable for this source".

    Returns:
        True when the layer may proceed to the count check.
    """
    if fields is not None and not fields:
        return False
    normalized_name = TITLE_SEPARATOR_RE.sub(" ", name or "")
    if NON_COMPREHENSIVE_SUBSET_RE.search(normalized_name) or NON_PRODUCTION_MARKERS_RE.search(normalized_name):
        return False
    return looks_like_parcel_data(name, fields)


def count_is_sufficient(count: int | None) -> bool:
    """Post-count-probe acceptance: enough data behind the layer to be a whole county.

    Args:
        count: The probed feature/row count, or None when the probe failed -
            an undetermined count never sinks an otherwise-good candidate.

    Returns:
        True when the count is unknown or clears :data:`MIN_PARCEL_FEATURE_COUNT`.
    """
    return count is None or count >= MIN_PARCEL_FEATURE_COUNT


def mentions_a_different_county(title: str, target_county_name: str) -> bool:
    """Whether a title names a specific county other than the one being searched for.

    A title naming no county at all (statewide datasets usually don't) is
    left unpenalized; only an explicit, different "<name> County" phrase is a
    signal. Used for ranking, never outright rejection.

    Args:
        title: The candidate item's own title.
        target_county_name: The jurisdiction being searched for
            (``PropertyJurisdiction.county_name``, e.g. ``"Skagit County"``).

    Returns:
        True when the title names a different county.
    """
    target_key = re.sub(r"\s*county\s*$", "", target_county_name, flags=re.IGNORECASE).strip().casefold()
    normalized_title = TITLE_SEPARATOR_RE.sub(" ", title)
    for match in COUNTY_NAME_IN_TITLE_RE.finditer(normalized_title):
        mentioned = match.group(1).strip().casefold()
        if mentioned and mentioned != target_key:
            return True
    return False


@cache
def _state_names_re() -> re.Pattern[str]:
    """Compiled-once alternation of every US state/territory full name, longest-first.

    Excludes a match immediately followed by "County": many states
    (Washington, Oregon, ...) are also common county names in unrelated
    states, and "Washington County Parcels, Minnesota" must not read as a
    reference to Washington state.
    """
    from urbanlens.dashboard.services.apis.property_records.jurisdiction import _STATE_ABBREVIATIONS

    names = sorted(_STATE_ABBREVIATIONS, key=len, reverse=True)
    return re.compile(r"\b(" + "|".join(re.escape(name) for name in names) + r")\b(?!\s+county)", re.IGNORECASE)


@cache
def _state_abbr_re() -> re.Pattern[str]:
    """Compiled-once alternation of every USPS state/territory abbreviation, comma-anchored.

    Case-sensitive and requires an immediately preceding comma (the standard
    "City, ST" / "County, ST" convention real search-result titles actually
    use - confirmed live: "Douglas County, OR" never once spells out
    "Oregon", so :func:`_state_names_re` alone missed the exact incident it
    was written for). A bare two-letter token alone is too likely to be an
    unrelated word or acronym ("OR", "IN", "ME", "HI", "OK", "GIS"...) to
    match without both the comma anchor and case sensitivity.
    """
    from urbanlens.dashboard.services.apis.property_records.jurisdiction import _STATE_NAMES

    abbrs = sorted(_STATE_NAMES, key=len, reverse=True)
    return re.compile(r",\s*(" + "|".join(re.escape(abbr) for abbr in abbrs) + r")\b")


def mentions_a_different_state(title: str, target_state_name: str) -> bool:
    """Whether a title names a specific US state other than the one being searched for.

    The state-level sibling of :func:`mentions_a_different_county`, needed
    because statewide datasets ("Florida Statewide Parcels") often name no
    county at all. Checks both the full state name and the "City/County, ST"
    abbreviation convention (see :func:`_state_abbr_re`) - real search
    results use both forms interchangeably.

    Args:
        title: The candidate item's own title.
        target_state_name: The full state name being searched for (resolve a
            ``PropertyJurisdiction.state`` abbreviation via
            ``jurisdiction.state_abbr_to_name`` first); '' disables the check.

    Returns:
        True when the title names a different state.
    """
    if not target_state_name:
        return False
    if any(match.group(1).casefold() != target_state_name.casefold() for match in _state_names_re().finditer(title)):
        return True
    from urbanlens.dashboard.services.apis.property_records.jurisdiction import _state_name_to_abbr

    target_abbr = _state_name_to_abbr(target_state_name)
    return any(match.group(1) != target_abbr for match in _state_abbr_re().finditer(title))


def arcgis_extent_and_wkid(extent: object) -> tuple[tuple[float, float, float, float], int] | None:
    """Extract an ArcGIS layer's own ``extent`` block as a box plus its native spatial-reference WKID.

    Deliberately does no unit conversion or reprojection here - real
    ArcGIS Online-hosted layers turn up in all sorts of projections (a
    live one used NAD83 Missouri state plane, wkid 26854, not the
    WGS-84/Web-Mercator pair this module might otherwise be tempted to
    special-case), and inverting an arbitrary projection correctly is not
    this module's job. The caller (``discovery._county_extent``) instead
    asks TIGERweb to reproject the *county's* extent into this same wkid,
    so the eventual overlap test (:func:`extent_overlaps_county`) always
    compares two boxes in one identical coordinate system.

    Args:
        extent: The raw ``extent`` object from a layer's ``?f=json`` body
            (``{"xmin": ..., "ymin": ..., "xmax": ..., "ymax": ...,
            "spatialReference": {"wkid": ...}}``), or anything else.

    Returns:
        ``((xmin, ymin, xmax, ymax), wkid)``, or None when the input is
        missing, malformed, or names no recognizable wkid.
    """
    if not isinstance(extent, dict):
        return None
    try:
        xmin, ymin, xmax, ymax = float(extent["xmin"]), float(extent["ymin"]), float(extent["xmax"]), float(extent["ymax"])
    except (KeyError, TypeError, ValueError):
        return None
    spatial_reference = extent.get("spatialReference")
    wkid = None
    if isinstance(spatial_reference, dict):
        wkid = spatial_reference.get("latestWkid") or spatial_reference.get("wkid")
    if not isinstance(wkid, int):
        return None
    return (xmin, ymin, xmax, ymax), wkid


def extent_overlaps_county(layer_extent: tuple[float, float, float, float] | None, county_extent: tuple[float, float, float, float] | None) -> bool:
    """Whether a candidate layer's own geographic extent overlaps the target county's.

    Built from a live-confirmed failure the name-based checks structurally
    can't catch: a Boone County, MO search's portal-search fallback accepted
    a layer plainly named "Parcels" (a canonical title, trusted on name
    alone) living inside a service named ``NicholasWV_AGOL``, owned by
    ``nicholas_assessor`` - Nicholas County, *West Virginia*'s own data,
    surfaced by ArcGIS Online's item-search for an unrelated "Boone County
    Missouri parcels" query. Nothing in that service's title says "County"
    or spells out a full state name, so neither
    :func:`mentions_a_different_county` nor :func:`mentions_a_different_state`
    had anything to match against. A layer's actual geographic extent has no
    such blind spot - it can't be obfuscated by an uninformative name.

    Either extent being unknown is permissive (True) - an undetermined
    location must never sink an otherwise-good candidate, matching every
    other probe in this module's "unknown never rejects" convention. Only a
    *confirmed* non-overlap (both extents known, and they don't intersect)
    rejects.

    Args:
        layer_extent: The candidate's own extent, ``(xmin, ymin, xmax, ymax)``
            (see :func:`arcgis_extent_and_wkid`).
        county_extent: The target county's real extent, reprojected into the
            *same* spatial reference as ``layer_extent`` (see
            ``CensusTigerwebGateway.get_county_extent``) - the two are only
            comparable when they share units.

    Returns:
        True when either extent is unknown, or the two boxes intersect.
    """
    if layer_extent is None or county_extent is None:
        return True
    l_xmin, l_ymin, l_xmax, l_ymax = layer_extent
    c_xmin, c_ymin, c_xmax, c_ymax = county_extent
    return l_xmin <= c_xmax and l_xmax >= c_xmin and l_ymin <= c_ymax and l_ymax >= c_ymin
