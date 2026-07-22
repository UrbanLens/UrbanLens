"""Internet Archive gateway - free, keyless, open full-text/media search.

https://archive.org/advancedsearch.php - distinct from this project's
existing Wayback Machine integration (URL snapshots): this searches every
item Internet Archive hosts (books, historical photos, newspapers, audio,
video) by keyword, matching against titles/descriptions/full text.

Relevance is the hard part here, not reach. ``advancedsearch.php`` does not
pass a bare query straight to its Solr index - it *rewrites* it, and the
rewrite is visible in the response's ``responseHeader.params.query``. A query
of ``Summit Road Cincinnati OH United States`` comes back as::

    ((text:Summit OR text:Summit OR text__reviews:Summit)
     (text:Road ...)(text:Cincinnati ...)(text:OH ...)
     (text:United ...)(text:States ...))
    OR (text:"Summit Road Cincinnati OH United States" OR ...)

Two things about that rewrite drove every choice in this module:

1. The default field is ``text`` - archive.org's **full text**, which includes
   OCR'd book bodies and broadcast/podcast transcripts. So the words merely
   have to occur *somewhere* inside a multi-hour radio programme for it to
   rank; the live query above returned Voice of America Africa broadcasts.
   That is the "irrelevant content from limited keywords in isolation"
   symptom. Fixed by scoping every clause to catalogue metadata fields
   (``title``/``subject``/``description``/``coverage``) instead.
2. The bare-word branch is OR'd against the phrase branch, so a *phrase*
   never actually constrains anything on its own. Fixed by building an
   explicit field-scoped boolean query, which the rewriter passes through
   untouched (verified against the live endpoint: a query that is already
   field-scoped comes back byte-identical in ``responseHeader.params.query``).

See ``LOCJsonGateway`` and ``SmithsonianGateway`` for the same class of fix
applied to the other two archive providers.
"""

from __future__ import annotations

from dataclasses import dataclass
import logging
import re
from typing import TYPE_CHECKING, Any, ClassVar

from urbanlens.dashboard.services.apis.assets.base import MediaItem, MediaProvider
from urbanlens.dashboard.services.locations.naming import contains_street_type_word, normalize_name_for_comparison

if TYPE_CHECKING:
    from collections.abc import Generator, Iterable, Sequence

logger = logging.getLogger(__name__)

_SEARCH_URL = "https://archive.org/advancedsearch.php"
_THUMBNAIL_URL = "https://archive.org/services/img/{identifier}"
_DETAILS_URL = "https://archive.org/details/{identifier}"

#: Fields requested from advancedsearch.php - keep in sync with ``_parse``.
#: ``subject`` is requested (not just queried) because ``_is_relevant``
#: re-checks the match locally rather than trusting the remote ranking.
_FIELDS = ("identifier", "title", "description", "date", "mediatype", "creator", "subject")

#: Restrict results to media types with a displayable preview image - excludes
#: books/audio/software/data noise that isn't useful in a photo gallery.
_MEDIA_TYPE_FILTER = "mediatype:(image OR movies)"

#: Collections excluded outright. Every one of these is a bulk ingest of
#: material that is *about* current events rather than about places, and whose
#: per-item ``subject`` terms are generated from captions - so a surname or a
#: place word mentioned once on air becomes an indexed subject and the item
#: matches a location search. The TV News Archive (``TV-NEWS``/``tvarchive``,
#: which is what the per-channel ``TV-CSPAN``/``TV-FOXNEWSW`` sets roll up to)
#: is the single biggest offender - a search for a location in Rosendale, NY
#: returned cable-news segments about a congressman named Rosendale.
#: ``deemphasize`` is archive.org's own marker for items it deliberately
#: down-ranks, so honouring it costs nothing.
_EXCLUDED_COLLECTIONS = ("TV-NEWS", "tvarchive", "altcensored", "fringe", "deemphasize")

#: Fields the *name* phrase must match. Deliberately excludes ``description``:
#: a description is free prose in which any incidental mention ("...not far
#: from Bannerman Castle...") produces a false positive, whereas a hit in
#: ``title`` or ``subject`` means the item is *catalogued as being about* the
#: thing. Verified against the live API: allowing ``description`` here is what
#: let a search for "Rosendale" + "New York" return items whose descriptions
#: merely name a person called Rosendale.
_NAME_FIELDS = ("title", "subject")

#: Fields the *locality* phrase may match. Broader than ``_NAME_FIELDS``
#: because the locality is only ever an additional narrowing clause on top of
#: an already-required name match, so an incidental mention is a useful signal
#: rather than a false positive. ``coverage`` is archive.org's Dublin Core
#: spatial-coverage field (e.g. ``["New York City (N.Y.)", "New York"]``) and
#: is the most likely place for a city/state to be catalogued properly.
_LOCALITY_FIELDS = ("title", "description", "subject", "coverage")

#: Pulls the quoted phrases back out of the search term built by
#: ``Pin.get_unique_search_name(quote_name=True, quote_locality=True)`` - i.e.
#: ``"Widow Jane Mine" "Rosendale New York"`` -> name, then locality.
_PHRASE_PATTERN = re.compile(r'"([^"]+)"')

#: Escaped inside a Lucene phrase literal. Everything else is inert once
#: wrapped in double quotes.
_LUCENE_PHRASE_ESCAPES = str.maketrans({'"': '\\"', "\\": "\\\\"})


def _first_str(value: Any) -> str:
    """Archive.org fields are inconsistently a bare string or a list of strings."""
    if isinstance(value, list):
        return str(value[0]) if value else ""
    return str(value) if value else ""


def _flatten(value: Any) -> str:
    """Collapse a string-or-list-of-strings field into one searchable string."""
    if isinstance(value, list):
        return " ".join(str(part) for part in value)
    return str(value) if value else ""


def _phrase_clause(fields: Sequence[str], phrase: str) -> str:
    """Build ``(field1:"phrase" OR field2:"phrase" ...)`` for one phrase.

    Args:
        fields: Metadata field names to match the phrase against.
        phrase: The literal phrase; quotes/backslashes are escaped.

    Returns:
        A parenthesised Lucene clause.
    """
    escaped = phrase.translate(_LUCENE_PHRASE_ESCAPES)
    return "(" + " OR ".join(f'{field}:"{escaped}"' for field in fields) + ")"


@dataclass(slots=True, kw_only=True)
class InternetArchiveGateway(MediaProvider):
    """Gateway for the Internet Archive's advancedsearch.php JSON API.

    Free, keyless, open-source project (archive.org). No rate limit is
    enforced by the API, but this project still tracks it like any other
    external call for admin visibility/throttling.
    """

    service_key: ClassVar[str] = "internet_archive"
    display_name: ClassVar[str] = "Internet Archive"
    paid_service: ClassVar[bool] = False
    # advancedsearch.php's relevance ranking treats space-separated words as
    # independent OR terms rather than requiring a phrase match, so a street
    # address - especially a generic street-type word ("Road", "Street") or a
    # bare house number - coincidentally matches unrelated items nationwide
    # (e.g. a location named/addressed "Summit Road" pulled in results about
    # US politics and National Archives records with no connection to the
    # actual place). Searching on name + city/state only is both more
    # selective and a better fit for how Internet Archive's collections are
    # catalogued (historical photos/documents rarely carry modern
    # street-address-level metadata anyway) - same fix, same reasoning,
    # already proven for the Library of Congress gateway's identical symptom
    # (see LOCJsonGateway.include_address).
    include_address: ClassVar[bool] = False
    # The name and the locality are each turned into one exact-phrase clause
    # (see the module docstring). Quoting them here is what lets
    # ``_split_search_term`` recover them as two distinct concepts from the
    # single string ``MediaPanelSource`` hands to ``_generate_media`` - an
    # unquoted term would be an undifferentiated bag of words, which is the
    # shape that produced the original noise.
    quote_name: ClassVar[bool] = True
    quote_locality: ClassVar[bool] = True
    # A bare "United States" adds nothing to a query already anchored on a
    # city/state phrase, and matches an enormous share of a US-heavy archive.
    search_with_country: ClassVar[bool] = False
    # A pin whose only "name" is its raw street address gives the name clause
    # no real narrowing power, so skip the provider rather than guarantee
    # noise - same reasoning as LOC/Smithsonian.
    reject_address_derived_names: ClassVar[bool] = True

    @staticmethod
    def _split_search_term(search_term: str) -> tuple[str, list[str]]:
        """Split the incoming query string into a name phrase and locality qualifiers.

        Args:
            search_term: The term built by ``MediaPanelSource.search_terms``,
                normally ``'"Name" "City ST"'``.

        Returns:
            Tuple of (name phrase, list of qualifier phrases). Falls back to
            treating the whole (unquoted) term as the name, so the gateway
            still behaves sensibly if called directly with a plain string.
        """
        phrases = [phrase.strip() for phrase in _PHRASE_PATTERN.findall(search_term)]
        phrases = [phrase for phrase in phrases if phrase]
        if not phrases:
            stripped = search_term.strip()
            return (stripped, [])
        return (phrases[0], phrases[1:])

    @classmethod
    def build_query(cls, name: str, qualifiers: Iterable[str] = ()) -> str:
        """Build the field-scoped Lucene query sent to advancedsearch.php.

        Every clause is ANDed, so a result must carry the name in its own
        catalogue metadata *and* satisfy every qualifier - unlike a bare
        keyword query, which archive.org expands into an OR over its full-text
        index (see the module docstring).

        Args:
            name: The location's name, matched as an exact phrase against
                ``_NAME_FIELDS``.
            qualifiers: Additional phrases (city/state) matched against
                ``_LOCALITY_FIELDS``.

        Returns:
            The complete ``q`` parameter value, or ``""`` when ``name`` is empty.
        """
        if not (name := name.strip()):
            return ""
        clauses = [_phrase_clause(_NAME_FIELDS, name)]
        clauses.extend(_phrase_clause(_LOCALITY_FIELDS, qualifier) for qualifier in qualifiers if qualifier.strip())
        clauses.append(_MEDIA_TYPE_FILTER)
        clauses.append("NOT collection:(" + " OR ".join(_EXCLUDED_COLLECTIONS) + ")")
        return " AND ".join(clauses)

    @staticmethod
    def _is_relevant(item: dict[str, Any], name: str) -> bool:
        """Re-check locally that a result really is catalogued under ``name``.

        The remote query already scopes the name to ``_NAME_FIELDS``, so this
        is defence in depth rather than the primary filter: archive.org
        rewrites queries server-side (see the module docstring) and has
        changed that behaviour before, and the analysed-field phrase match is
        looser than a literal one. Re-testing the raw metadata makes the
        relevance guarantee this project's rather than the remote engine's,
        and keeps it verifiable without a network call.

        Args:
            item: A normalized result dict from ``search``.
            name: The name phrase the query was built from.

        Returns:
            True when the name appears in the item's title or subject terms.
        """
        if not (needle := normalize_name_for_comparison(name)):
            return False
        haystack = normalize_name_for_comparison(f"{item.get('title') or ''} {_flatten(item.get('subject'))}")
        return needle in haystack

    def search(self, query: str, *, rows: int = 20) -> list[dict[str, Any]]:
        """Run one already-built Lucene query against advancedsearch.php.

        Args:
            query: A complete, field-scoped query - build it with
                :meth:`build_query` rather than passing free text, which
                archive.org expands into a full-text OR search.
            rows: Maximum number of results to request.

        Returns:
            List of normalized dicts with keys ``identifier``, ``title``,
            ``description``, ``date``, ``mediatype``, ``creator``, ``subject``.
        """
        if not query:
            return []
        params: dict[str, Any] = {"q": query, "fl[]": list(_FIELDS), "rows": rows, "output": "json"}
        response = self.session.get(_SEARCH_URL, params=params, timeout=(5, 15))
        response.raise_for_status()
        docs = (response.json().get("response") or {}).get("docs") or []
        return [
            {
                "identifier": doc.get("identifier") or "",
                "title": doc.get("title") or "",
                "description": doc.get("description") or "",
                "date": _first_str(doc.get("date"))[:10],
                "mediatype": doc.get("mediatype") or "",
                "creator": _first_str(doc.get("creator")),
                "subject": doc.get("subject") or [],
            }
            for doc in docs
            if doc.get("identifier")
        ]

    def _generate_media(self, search_term: str, address: str | None = None) -> Generator[MediaItem]:
        """Yield items catalogued under this location's name (photos, film).

        A name that contains a street-type word ("Summit Road") names a road
        in every state, so it is only searched together with the location's
        city/state - and not at all when no locality is known, since the
        broad query would be guaranteed noise. A distinctive name
        ("Bannerman Castle") is searched with the locality first and, if that
        finds nothing, on its own: archive.org's catalogue rarely records a
        city/state for historical photographs, so requiring one would discard
        genuine matches.
        """
        name, qualifiers = self._split_search_term(search_term)
        if not name:
            return

        generic_name = contains_street_type_word(name)
        if generic_name and not qualifiers:
            logger.debug("internet_archive: skipping %r - street-type name with no locality to narrow it", name)
            return

        attempts: list[list[str]] = [qualifiers]
        if qualifiers and not generic_name:
            attempts.append([])

        seen: set[str] = set()
        for attempt in attempts:
            results = [item for item in self.search(self.build_query(name, attempt)) if self._is_relevant(item, name)]
            for item in results:
                identifier = item["identifier"]
                if identifier in seen:
                    continue
                seen.add(identifier)
                description = _flatten(item.get("description"))
                yield MediaItem(
                    url=_DETAILS_URL.format(identifier=identifier),
                    thumb_url=_THUMBNAIL_URL.format(identifier=identifier),
                    caption=item.get("title") or item.get("creator") or description[:120] or identifier,
                    source=self.display_name,
                    page_url=_DETAILS_URL.format(identifier=identifier),
                )
            if seen:
                return
