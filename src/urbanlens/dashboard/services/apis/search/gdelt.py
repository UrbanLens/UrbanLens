"""GDELT gateway - free, keyless geocoded global news search.

https://blog.gdeltproject.org/gdelt-doc-2-0-api-debuts/ - the GDELT Project's
DOC 2.0 API searches worldwide online news coverage by keyword, going back to
2017 (and further via GDELT 1.0). No API key is required.
"""

from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import Any, ClassVar

import requests

from urbanlens.dashboard.services.gateway import Gateway

logger = logging.getLogger(__name__)

_DOC_API_URL = "https://api.gdeltproject.org/api/v2/doc/doc"


def _normalize_article(article: dict[str, Any]) -> dict[str, Any]:
    """Flatten one GDELT DOC API article record into a display-friendly dict."""
    seen_date = article.get("seendate") or ""
    # GDELT's seendate is compact UTC "YYYYMMDDTHHMMSSZ"; only the date part is shown.
    date = f"{seen_date[0:4]}-{seen_date[4:6]}-{seen_date[6:8]}" if len(seen_date) >= 8 else ""
    return {
        "title": article.get("title") or "",
        "url": article.get("url") or "",
        "domain": article.get("domain") or "",
        "date": date,
        "source_country": article.get("sourcecountry") or "",
    }


def _normalize_tonechart(bins: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Reduce a GDELT tonechart histogram to a single count-weighted average tone."""
    total_count = sum(int(tone_bin.get("count") or 0) for tone_bin in bins)
    if not total_count:
        return None
    weighted_sum = sum(int(tone_bin.get("bin") or 0) * int(tone_bin.get("count") or 0) for tone_bin in bins)
    return {"average_tone": round(weighted_sum / total_count, 1), "article_count": total_count}


@dataclass(slots=True, kw_only=True)
class GdeltGateway(Gateway):
    """Gateway for the GDELT Project's DOC 2.0 news search API."""

    service_key: ClassVar[str] = "gdelt"
    paid_service: ClassVar[bool] = False

    def search_articles(self, query: str, *, limit: int = 10) -> list[dict[str, Any]]:
        """Search worldwide online news coverage by keyword.

        Args:
            query: Free-text search query. Multi-word phrases should be
                pre-quoted by the caller (GDELT's query language treats an
                unquoted phrase as an AND of separate words).
            limit: Maximum number of articles to return (1-250).

        Returns:
            Normalized article dicts, most relevant first; empty when
            nothing matched or the request failed.
        """
        if not query:
            return []
        params: dict[str, Any] = {
            "query": query,
            "mode": "artlist",
            "format": "json",
            "maxrecords": max(1, min(int(limit), 250)),
            "sort": "hybridrel",
        }
        try:
            response = self.session.get(_DOC_API_URL, params=params, timeout=15)
            response.raise_for_status()
            body = response.json()
        except requests.exceptions.RequestException:
            logger.warning("GDELT search failed for %r", query, exc_info=True)
            return []
        except ValueError:
            # GDELT returns plain-text (not JSON) for some malformed queries.
            logger.warning("GDELT returned a non-JSON response for %r", query)
            return []
        return [_normalize_article(article) for article in body.get("articles") or []]

    def get_tone_summary(self, query: str) -> dict[str, Any] | None:
        """Return an aggregate sentiment/tone summary of news coverage for a query.

        Uses GDELT's ``tonechart`` mode: a histogram of article tone scores
        (roughly -100..+100, negative meaning more negative sentiment) for
        the same query used by ``search_articles``. A coarse "is coverage of
        this place skewing negative" signal - e.g. disaster/crime/hazard
        reporting versus routine local-interest coverage.

        Args:
            query: Free-text search query, same format as ``search_articles``.

        Returns:
            Dict with ``average_tone`` (count-weighted mean of the histogram
            bins) and ``article_count`` (total articles across all bins), or
            None when nothing matched or the request failed.
        """
        if not query:
            return None
        params: dict[str, Any] = {"query": query, "mode": "tonechart", "format": "json"}
        try:
            response = self.session.get(_DOC_API_URL, params=params, timeout=15)
            response.raise_for_status()
            body = response.json()
        except requests.exceptions.RequestException:
            logger.warning("GDELT tone chart lookup failed for %r", query, exc_info=True)
            return None
        except ValueError:
            logger.warning("GDELT returned a non-JSON response for tone chart %r", query)
            return None
        return _normalize_tonechart(body.get("tonechart") or [])
