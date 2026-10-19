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

if TYPE_CHECKING:
    from django.contrib.auth.models import User

    from urbanlens.dashboard.models.location.model import Location

#: LocationCache source, distinct from the trusted ``web_search`` rows.
USER_PROVIDED_SOURCE = "web_search_user_provided"

MAX_BODY_BYTES = 32 * 1024
MAX_RESULTS_PER_POST = 20
MAX_STORED_RESULTS = 30
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


def store_user_provided_results(location: Location, user: User, query_key: str, results: list[SubmittedResult]) -> int:
    """
    Merge validated results into the location's user-provided cache row.

    Each stored result keeps who submitted it and when, and the row is flagged ``trust: "user_provided"``.
    A URL already stored keeps its original submitter; a row cached for a different query is replaced.

    Args:
        location: The shared Location the pin sits at.
        user: The submitting user.
        query_key: The query the server derived for the pin, never one the client chose.
        results: Validated results.

    Returns:
        How many results the row holds afterwards.
    """
    now = timezone.now().isoformat()
    fresh = [{"link": r.url, "title": r.title, "snippet": r.snippet, "submitted_by": user.pk, "submitted_at": now} for r in results]
    with transaction.atomic():
        entry = LocationCache.objects.select_for_update().filter(location=location, source=USER_PROVIDED_SOURCE).first()
        existing: list[dict[str, Any]] = []
        if entry is not None and entry.query_key == query_key and not entry.is_stale:
            existing = list(entry.data.get("results", []))
        known = {r["link"] for r in existing}
        merged = [r for r in fresh if r["link"] not in known] + existing
        merged = merged[:MAX_STORED_RESULTS]
        LocationCache.set(location, USER_PROVIDED_SOURCE, {"trust": "user_provided", "results": merged}, query_key=query_key)
    return len(merged)


def load_user_provided_results(location: Location | None, query_key: str, exclude_links: set[str]) -> list[dict[str, Any]]:
    """The fresh user-provided results for this query, minus links the trusted results already show."""
    if location is None:
        return []
    entry = LocationCache.get_fresh(location, USER_PROVIDED_SOURCE)
    if entry is None or entry.query_key != query_key:
        return []
    out = []
    for r in entry.data.get("results", []):
        link = r.get("link")
        if isinstance(link, str) and link not in exclude_links:
            out.append({"link": link, "title": r.get("title", ""), "snippet": r.get("snippet", ""), "domain": (urlsplit(link).hostname or "").removeprefix("www.")})
    return out
