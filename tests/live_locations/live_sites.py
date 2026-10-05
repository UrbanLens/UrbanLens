"""The live-locations suite's catalogue, REData client and per-site answer cache.

Kept free of Django so the REData half of the suite runs against any deployment from any
checkout; the pipeline half imports Django itself.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import os
from pathlib import Path
import re
import time
import tomllib
from typing import Any

import requests

CATALOGUE = Path(__file__).with_name("kirkbrides.toml")

#: Words that make a name an institution's rather than a place's or a person's.
_INSTITUTION_WORDS = frozenset(
    {
        "asylum",
        "campus",
        "center",
        "centre",
        "complex",
        "facility",
        "hospital",
        "institute",
        "institution",
        "sanatorium",
    }
)

#: REData error codes and provider statuses that mean "not established", never "nothing there".
_RETRYABLE_ERRORS = frozenset(
    {
        "rate_limited", "search_unavailable", "all_providers_unavailable", "upstream_unavailable",
        "source_rate_limited", "source_error",
    }
)  # fmt: skip
_UNANSWERED_STATUSES = frozenset({"rate_limited", "unavailable", "not_cached"})


@dataclass(frozen=True, slots=True)
class Site:
    """One campus in ``kirkbrides.toml``."""

    key: str
    name: str
    wikipedia: str
    place: str
    state: str
    lat: float
    lng: float
    built: int
    status: str
    nrhp: str = ""
    aliases: tuple[str, ...] = ()
    tier: str = "catalogue"
    min_buildings: int | None = None
    known_issues: dict[str, str] = field(default_factory=dict)

    @property
    def standing(self) -> bool:
        return self.status != "demolished"

    @property
    def town(self) -> str:
        return self.place.split(",")[0]

    @property
    def required_buildings(self) -> int:
        if self.min_buildings is not None:
            return self.min_buildings
        return 10 if self.tier == "primary" else 3

    @property
    def labels(self) -> tuple[str, ...]:
        return (self.name, self.wikipedia, *self.aliases)

    @property
    def qualifiers(self) -> tuple[str, ...]:
        """Phrases that tell this campus from others of the same name: its town, and its wikipedia disambiguator."""
        found = [_normalized(self.town)]
        if match := re.search(r"\(([^)]+)\)", self.wikipedia):
            found.append(_normalized(match.group(1)))
        return tuple(phrase for phrase in found if phrase)

    def mentions(self, *texts: Any) -> bool:
        """Whether any text names this site.

        One of its names must appear as a whole phrase. A name that does not say it is an institution
        ("The Ridges"), or that other campuses share (a wikipedia title disambiguated in parentheses),
        also needs the town or the disambiguator somewhere in the same texts - not necessarily next to it,
        so "Athens, Georgia ... the ridges" still counts for Athens, Ohio.
        """
        haystack = f" {_normalized(' '.join(str(text) for text in texts if text))} "
        qualified = any(f" {phrase} " in haystack for phrase in self.qualifiers)
        shared = "(" in self.wikipedia
        for label in self.labels:
            phrase = _normalized(re.sub(r"\([^)]*\)", "", label))
            if not phrase or f" {phrase} " not in haystack:
                continue
            if qualified or not (shared or _INSTITUTION_WORDS.isdisjoint(phrase.split())):
                return True
        return False


def _normalized(text: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", text.lower()))


def load_sites(path: Path = CATALOGUE) -> list[Site]:
    with path.open("rb") as handle:
        raw = tomllib.load(handle)
    return [Site(**{**entry, "aliases": tuple(entry.get("aliases", ()))}) for entry in raw["site"]]


def select_sites(sites: list[Site], selector: str) -> list[Site]:
    """``primary`` (default), ``all``, or a comma-separated list of keys."""
    if selector in {"", "primary"}:
        return [site for site in sites if site.tier == "primary"]
    if selector == "all":
        return sites
    keys = {key.strip() for key in selector.split(",") if key.strip()}
    unknown = keys - {site.key for site in sites}
    if unknown:
        raise ValueError(f"Unknown site keys: {', '.join(sorted(unknown))}")
    return [site for site in sites if site.key in keys]


class InconclusiveError(Exception):
    """REData could not establish an answer (budget, upstream outage) within the wait allowed."""


@dataclass(slots=True)
class Answer:
    status: int
    body: Any
    seconds: float

    @property
    def rows(self) -> list[dict[str, Any]]:
        if isinstance(self.body, list):
            return self.body
        if isinstance(self.body, dict) and isinstance(self.body.get("results"), list):
            return self.body["results"]
        return []

    @property
    def unanswered_providers(self) -> list[str]:
        providers = self.body.get("providers") if isinstance(self.body, dict) else None
        return [entry["provider"] for entry in providers or [] if entry.get("status") in _UNANSWERED_STATUSES]


class LiveRedata:
    """A REData client that waits out budget refusals and memoizes every answer for the session.

    Every check reads the same answer for an endpoint, so a site costs one call per endpoint
    however many checks look at it.
    """

    def __init__(self, base_url: str, api_key: str, *, host: str = "", max_wait_seconds: float = 180.0) -> None:
        self.base_url = base_url.rstrip("/") + "/api/v1/"
        self.session = requests.Session()
        self.session.headers["Authorization"] = f"Bearer {api_key}"
        self.session.headers["Accept"] = "application/json"
        if host:
            # A deployment reached by address rather than its own name: Django checks the Host,
            # and REData redirects plain HTTP unless told the hop before it was TLS.
            self.session.headers["Host"] = host
            self.session.headers["X-Forwarded-Proto"] = "https"
        self.max_wait_seconds = max_wait_seconds
        self._answers: dict[tuple[str, tuple[tuple[str, str], ...]], Answer] = {}
        self.log: list[dict[str, Any]] = []

    @classmethod
    def from_env(cls) -> LiveRedata | None:
        url = os.environ.get("UL_LIVE_REDATA_API_URL", "").strip()
        key = os.environ.get("UL_LIVE_REDATA_API_KEY", "").strip()
        if not (url and key):
            return None
        return cls(
            url,
            key,
            host=os.environ.get("UL_LIVE_REDATA_HOST", "").strip(),
            max_wait_seconds=float(os.environ.get("UL_LIVE_MAX_WAIT_SECONDS", "180")),
        )

    def reachable(self) -> bool:
        try:
            return self.session.get(self.base_url + "capabilities/", timeout=10).status_code == 200
        except requests.RequestException:
            return False

    def get(self, path: str, **params: Any) -> Answer:
        """GET ``path``, waiting out refusals that say "ask again later".

        Raises:
            InconclusiveError: Still refused when the wait allowed runs out.
        """
        key = (path, tuple(sorted((name, str(value)) for name, value in params.items())))
        if key in self._answers:
            return self._answers[key]
        deadline = time.monotonic() + self.max_wait_seconds
        while True:
            started = time.monotonic()
            response = self.session.get(self.base_url + path, params=params, timeout=300)
            seconds = time.monotonic() - started
            try:
                body: Any = response.json()
            except ValueError:
                body = response.text[:500]
            self.log.append(
                {"path": path, "params": params, "status": response.status_code, "seconds": round(seconds, 2)}
            )
            error = body.get("error") if isinstance(body, dict) else None
            if _asks_again_later(response, error):
                wait = _retry_after(response, body)
                if time.monotonic() + wait > deadline:
                    raise InconclusiveError(
                        f"{path}: {error or response.status_code} for longer than {self.max_wait_seconds:.0f}s"
                    )
                time.sleep(wait)
                continue
            answer = Answer(response.status_code, body, seconds)
            if response.status_code < 500:
                self._answers[key] = answer
            return answer


def _asks_again_later(response: requests.Response, error: Any) -> bool:
    if response.status_code == 429:
        return True
    return response.status_code in {502, 503, 504} and (
        error in _RETRYABLE_ERRORS or "Retry-After" in response.headers or not isinstance(error, str)
    )


def _retry_after(response: requests.Response, body: Any) -> float:
    header = response.headers.get("Retry-After", "")
    if header.isdigit():
        return min(float(header), 120.0)
    message = body.get("message", "") if isinstance(body, dict) else ""
    if match := re.search(r"(\d+)\s*s\b", message):
        return min(float(match.group(1)) + 1, 120.0)
    return 15.0
