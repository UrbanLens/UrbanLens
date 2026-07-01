"""Fetch and cache GitHub repository contributors for the thanks page."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import logging
from typing import Any

from django.core.cache import cache
import requests

from urbanlens.core.cache_keys import make_cache_key

logger = logging.getLogger(__name__)

GITHUB_REPO_OWNER = "UrbanLens"
GITHUB_REPO_NAME = "UrbanLens"
GITHUB_REPO_SLUG = f"{GITHUB_REPO_OWNER}/{GITHUB_REPO_NAME}"
GITHUB_REPO_URL = f"https://github.com/{GITHUB_REPO_SLUG}"
_GITHUB_API_URL = f"https://api.github.com/repos/{GITHUB_REPO_SLUG}/contributors"

_USER_AGENT = "UrbanLens/1.0 (https://github.com/UrbanLens/UrbanLens; hello@urbanlens.org) python-requests"
_CACHE_KEY = make_cache_key("github_contributors", GITHUB_REPO_SLUG)
_CACHE_TTL_SECONDS = 86_400  # 24 hours
_PAGE_SIZE = 100


@dataclass(frozen=True, slots=True)
class GitHubContributor:
    """A GitHub user who has contributed to the UrbanLens repository."""

    login: str
    profile_url: str
    avatar_url: str
    contributions: int


def get_github_contributors() -> list[GitHubContributor]:
    """Return cached GitHub contributors, refreshing from the API when stale.

    Returns:
        Contributors sorted by contribution count (highest first). On API
        failure, returns the last cached list when available, otherwise an
        empty list.
    """
    cached = cache.get(_CACHE_KEY)
    if cached is not None:
        return [_contributor_from_dict(item) for item in cached]

    try:
        contributors = _fetch_contributors()
    except requests.RequestException:
        logger.exception("Failed to fetch GitHub contributors for %s", GITHUB_REPO_SLUG)
        return []

    cache.set(_CACHE_KEY, [asdict(item) for item in contributors], _CACHE_TTL_SECONDS)
    return contributors


def _contributor_from_dict(data: dict[str, Any]) -> GitHubContributor:
    """Build a ``GitHubContributor`` from a cached dict payload."""
    return GitHubContributor(
        login=str(data["login"]),
        profile_url=str(data["profile_url"]),
        avatar_url=str(data["avatar_url"]),
        contributions=int(data["contributions"]),
    )


def _fetch_contributors() -> list[GitHubContributor]:
    """Download all contributor pages from the GitHub REST API.

    Returns:
        Parsed contributors excluding GitHub bot accounts.

    Raises:
        requests.RequestException: When the GitHub API request fails.
    """
    session = requests.Session()
    session.headers.update(
        {
            "Accept": "application/vnd.github+json",
            "User-Agent": _USER_AGENT,
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )

    contributors: list[GitHubContributor] = []
    page = 1

    while True:
        response = session.get(
            _GITHUB_API_URL,
            params={"anon": "false", "per_page": _PAGE_SIZE, "page": page},
            timeout=15,
        )
        response.raise_for_status()
        payload: list[dict[str, Any]] = response.json()
        if not payload:
            break

        for item in payload:
            login = str(item.get("login") or "")
            if not login or login.endswith("[bot]"):
                continue
            contributors.append(
                GitHubContributor(
                    login=login,
                    profile_url=str(item.get("html_url") or f"https://github.com/{login}"),
                    avatar_url=str(item.get("avatar_url") or ""),
                    contributions=int(item.get("contributions") or 0),
                ),
            )

        if len(payload) < _PAGE_SIZE:
            break
        page += 1

    return contributors
