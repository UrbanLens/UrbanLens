"""
Web-search results found by a viewer's own browser, posted back and cached with low trust.

The browser runs Google's Programmable Search Element, then posts what it found. Nothing in the
payload can be verified, and other users see it afterwards, so every field is validated, reduced to
plain text and stored apart from the site's own search results under its own cache source.
"""

from __future__ import annotations

from dataclasses import dataclass
import html
import re
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from django.db import transaction
from django.utils import timezone

from urbanlens.dashboard.models.cache.location_cache import LocationCache
from urbanlens.dashboard.services.search.corroboration import CORROBORATION, readable_by

if TYPE_CHECKING:
    from django.contrib.auth.models import User

    from urbanlens.dashboard.models.location.model import Location

#: LocationCache source, distinct from the trusted ``web_search`` rows.
USER_PROVIDED_SOURCE = "web_search_user_provided"

MAX_BODY_BYTES = 32 * 1024
MAX_RESULTS_PER_POST = 20
#: Entries one account may be first to submit; its oldest uncorroborated ones go first when it posts more.
MAX_RESULTS_PER_ACCOUNT = 10
#: Entries one row holds; the account holding the most gives up its oldest first.
MAX_STORED_RESULTS = 100
MAX_URL_LENGTH = 2048
MAX_TITLE_LENGTH = 200
MAX_SNIPPET_LENGTH = 500

_ALLOWED_RESULT_KEYS = frozenset({"url", "title", "snippet"})
_TAG_RE = re.compile(r"<[^>]*>")
_CONTROL_RE = re.compile("[\x00-\x1f\x7f-\x9f\u2028\u2029]")
_WHITESPACE_RE = re.compile(r"\s+")


class InvalidSubmissionError(ValueError):
    """The posted payload is malformed or breaks a limit; the message is safe to show the caller."""


@dataclass(frozen=True, slots=True)
class SubmittedResult:
    """One validated result: a plain-text title and snippet, and an http(s) URL."""

    url: str
    title: str
    snippet: str


def _plain_text(value: object, field: str, limit: int) -> str:
    """Reduce *value* to bounded single-line plain text; markup is dropped, never stored."""
    if value is None:
        return ""
    if not isinstance(value, str):
        raise InvalidSubmissionError(f"{field} must be a string")
    text = html.unescape(_TAG_RE.sub(" ", value))
    # A second pass: unescaping can reveal "&lt;b&gt;" as tags.
    text = _TAG_RE.sub(" ", text).replace("<", " ").replace(">", " ")
    text = _WHITESPACE_RE.sub(" ", _CONTROL_RE.sub(" ", text)).strip()
    return text[:limit]


def _clean_url(value: object) -> str:
    if not isinstance(value, str) or not value or len(value) > MAX_URL_LENGTH:
        raise InvalidSubmissionError("url must be a string of at most 2048 characters")
    if _CONTROL_RE.search(value) or any(ch.isspace() for ch in value):
        raise InvalidSubmissionError("url contains whitespace or control characters")
    try:
        parts = urlsplit(value)
        hostname = parts.hostname
        parts.port  # noqa: B018 - raises ValueError on a malformed port
    except ValueError as exc:
        raise InvalidSubmissionError("url is malformed") from exc
    if parts.scheme not in {"http", "https"} or not hostname:
        raise InvalidSubmissionError("url must be http or https")
    if parts.username is not None or parts.password is not None:
        raise InvalidSubmissionError("url must not carry credentials")
    return value


def validate_submission(payload: object) -> list[SubmittedResult]:
    """
    Check a decoded postback body.

    Args:
        payload: The decoded JSON, expected to be ``{"results": [{"url", "title", "snippet"}, ...]}``.

    Returns:
        The validated results, de-duplicated by URL, in submitted order.

    Raises:
        InvalidSubmissionError: On any structural problem, unknown field, or limit breach.
    """
    if not isinstance(payload, dict) or set(payload) != {"results"}:
        raise InvalidSubmissionError("body must be an object with only a results list")
    raw = payload["results"]
    if not isinstance(raw, list):
        raise InvalidSubmissionError("results must be a list")
    if not raw:
        raise InvalidSubmissionError("results is empty")
    if len(raw) > MAX_RESULTS_PER_POST:
        raise InvalidSubmissionError(f"at most {MAX_RESULTS_PER_POST} results per post")
    seen: set[str] = set()
    cleaned: list[SubmittedResult] = []
    for item in raw:
        if not isinstance(item, dict):
            raise InvalidSubmissionError("each result must be an object")
        if not set(item) <= _ALLOWED_RESULT_KEYS or "url" not in item:
            raise InvalidSubmissionError("each result takes url, title and snippet only")
        url = _clean_url(item["url"])
        if url in seen:
            continue
        seen.add(url)
        title = _plain_text(item.get("title"), "title", MAX_TITLE_LENGTH)
        cleaned.append(SubmittedResult(url=url, title=title or urlsplit(url).hostname or url, snippet=_plain_text(item.get("snippet"), "snippet", MAX_SNIPPET_LENGTH)))
    return cleaned


def canonical_url(url: str) -> str:
    """
    The URL with what does not change the page dropped, so two accounts' copies of one link match.

    Args:
        url: A validated http(s) URL.

    Returns:
        It lower-cased in scheme and host, without ``www.``, default port, fragment or trailing slash.
    """
    parts = urlsplit(url)
    host = (parts.hostname or "").lower().removeprefix("www.")
    default_port = {"http": 80, "https": 443}.get(parts.scheme.lower())
    if parts.port is not None and parts.port != default_port:
        host = f"{host}:{parts.port}"
    path = parts.path.rstrip("/")
    return f"{parts.scheme.lower()}://{host}{path}{'?' + parts.query if parts.query else ''}"


def _submitters(entry: dict[str, Any]) -> list[int]:
    """Who submitted a stored entry, tolerating the earlier single-id shape."""
    raw = entry.get("submitted_by")
    if isinstance(raw, int):
        return [raw]
    return [x for x in raw if isinstance(x, int)] if isinstance(raw, list) else []


def _enforce_caps(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    Drop the oldest entries of any account over its share, then of the heaviest account while the row is over its cap.

    ``entries`` is newest first. An uncorroborated entry goes before a corroborated one of the same account.
    """

    def owner(entry: dict[str, Any]) -> int | None:
        who = _submitters(entry)
        return who[0] if who else None

    def evict_from(account: int | None) -> None:
        mine = [e for e in entries if owner(e) == account]
        victim = next((e for e in reversed(mine) if len(set(_submitters(e))) < CORROBORATION), mine[-1])
        entries.remove(victim)

    for account in {owner(e) for e in entries}:
        while sum(1 for e in entries if owner(e) == account) > MAX_RESULTS_PER_ACCOUNT:
            evict_from(account)
    while len(entries) > MAX_STORED_RESULTS:
        counts: dict[int | None, int] = {}
        for e in entries:
            counts[owner(e)] = counts.get(owner(e), 0) + 1
        evict_from(max(counts, key=lambda a: counts[a]))
    return entries


def store_user_provided_results(location: Location, user: User, query_key: str, results: list[SubmittedResult], audience: str = "") -> int:
    """
    Merge validated results into the user-provided cache row of one search.

    Each stored result keeps who submitted it (``submitted_by``, every account that posted the same canonical
    URL) and when it was first posted, and the row is flagged ``trust: "user_provided"``. A URL already stored
    gains the poster as a submitter and keeps its text; a row cached for a different query is replaced. One
    account cannot displace another's entries: it holds at most ``MAX_RESULTS_PER_ACCOUNT`` first submissions.

    Args:
        location: The shared Location the pin sits at.
        user: The submitting user.
        query_key: The ``LocationCache``-sized query the server derived for the pin, never one the client chose.
        results: Validated results.
        audience: The search's audience; a search built from the owner's own names is read only under it.

    Returns:
        How many results the row holds afterwards.
    """
    now = timezone.now().isoformat()
    with transaction.atomic():
        entry = LocationCache.objects.select_for_update().filter(location=location, source=USER_PROVIDED_SOURCE, audience=audience).first()
        existing: list[dict[str, Any]] = []
        if entry is not None and entry.query_key == query_key and not entry.is_stale:
            existing = [dict(r) for r in entry.data.get("results", []) if isinstance(r, dict) and isinstance(r.get("link"), str)]
        by_key = {canonical_url(r["link"]): r for r in existing}
        added: list[dict[str, Any]] = []
        for r in results:
            known = by_key.get(canonical_url(r.url))
            if known is not None:
                submitters = _submitters(known)
                if user.pk not in submitters:
                    known["submitted_by"] = [*submitters, user.pk]
                continue
            fresh = {"link": r.url, "title": r.title, "snippet": r.snippet, "submitted_by": [user.pk], "submitted_at": now}
            by_key[canonical_url(r.url)] = fresh
            added.append(fresh)
        merged = _enforce_caps(added + existing)
        LocationCache.set(location, USER_PROVIDED_SOURCE, {"trust": "user_provided", "results": merged}, query_key=query_key, audience=audience)
    return len(merged)


def load_user_provided_results(location: Location | None, viewer_id: int | None, query_key: str, exclude_links: set[str], audience: str = "") -> list[dict[str, Any]]:
    """
    The fresh user-provided results a viewer may see for one search.

    A result is shown to the accounts that submitted it, and to everyone else only once
    ``CORROBORATION`` distinct accounts have submitted it.

    Args:
        location: The pin's Location.
        viewer_id: The viewing user's id.
        query_key: The truncated query key the search was stored under.
        exclude_links: Links the site's own results already show.
        audience: The search's audience.

    Returns:
        Display dicts with ``link``, ``title``, ``snippet`` and ``domain``.
    """
    if location is None:
        return []
    entry = LocationCache.get_fresh(location, USER_PROVIDED_SOURCE, audience)
    if entry is None or entry.query_key != query_key:
        return []
    excluded = {canonical_url(link) for link in exclude_links if link.startswith(("http://", "https://"))} | exclude_links
    out = []
    for r in entry.data.get("results", []):
        link = r.get("link")
        if not isinstance(link, str) or link in excluded or canonical_url(link) in excluded:
            continue
        if not readable_by(_submitters(r), viewer_id):
            continue
        out.append({"link": link, "title": r.get("title", ""), "snippet": r.get("snippet", ""), "domain": (urlsplit(link).hostname or "").removeprefix("www.")})
    return out
