"""POST /csp-report/ - where browsers send Content-Security-Policy violation reports.

Reports are logged, not stored: with the policy enforced, a violation is something a page lost, and
the log is where a deployment already looks for that. Every field is attacker-writable, so each is
cut to its origin and path, stripped of control characters and truncated before it is logged.
"""

from __future__ import annotations

import json
import logging
import re
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from django.http import HttpResponse
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.csrf import csrf_exempt

from urbanlens.dashboard.services.security.throttle import Rate

if TYPE_CHECKING:
    from collections.abc import Iterator

    from django.http import HttpRequest

logger = logging.getLogger(__name__)

#: A browser batches a page's violations; a handful of reports is a few kilobytes.
MAX_REPORT_BYTES = 64 * 1024

#: Per address. A page with a violation reports it on every load, so this is a budget for pages
#: opened, and anything past it is the same few reports again.
CSP_REPORT_RATE = Rate(limit=60, window_seconds=300)

_MAX_FIELD = 200
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")

#: The two spellings: ``report-uri``'s ``csp-report`` object, and the Reporting API's ``body``.
_FIELDS = {
    "directive": ("effective-directive", "effectiveDirective", "violated-directive"),
    "blocked": ("blocked-uri", "blockedURL"),
    "document": ("document-uri", "documentURL"),
    "source": ("source-file", "sourceFile"),
    "disposition": ("disposition",),
}


def _clean(value: object) -> str:
    """One report field as it may be logged: no query, no fragment, no control characters, bounded.

    Args:
        value: The raw field.

    Returns:
        The URL's scheme, host and path, or the keyword (``inline``, ``eval``) as sent.
    """
    text = _CONTROL.sub(" ", str(value)) if value is not None else ""
    parts = urlsplit(text)
    if parts.scheme in {"http", "https"} and parts.netloc:
        text = f"{parts.scheme}://{parts.netloc}{parts.path}"
    return text[:_MAX_FIELD]


def _pick(report: dict[str, Any], names: tuple[str, ...]) -> str:
    for name in names:
        if report.get(name):
            return _clean(report[name])
    return ""


def iter_reports(payload: object) -> Iterator[dict[str, Any]]:
    """The violation reports in a request body, in either wire format.

    Args:
        payload: The decoded JSON body.

    Yields:
        Each report's fields, other report types skipped.
    """
    if isinstance(payload, dict) and isinstance(payload.get("csp-report"), dict):
        yield payload["csp-report"]
        return
    if isinstance(payload, list):
        for entry in payload:
            if isinstance(entry, dict) and entry.get("type") == "csp-violation" and isinstance(entry.get("body"), dict):
                yield entry["body"]


@method_decorator(csrf_exempt, name="dispatch")
class CspReportView(View):
    """Logs each violation a browser reports, one warning per report. Throttled in the URLconf."""

    http_method_names = ["post"]

    def post(self, request: HttpRequest) -> HttpResponse:
        """Accept a report batch.

        Args:
            request: The browser's report POST.

        Returns:
            204 once logged, 400 for a body that is not JSON, 413 past the size limit.
        """
        length = request.META.get("CONTENT_LENGTH") or "0"
        if not length.isdigit() or int(length) > MAX_REPORT_BYTES:
            return HttpResponse(status=413)
        try:
            payload = json.loads(request.body)
        except ValueError:
            return HttpResponse(status=400)
        for report in iter_reports(payload):
            fields = {key: _pick(report, names) for key, names in _FIELDS.items()}
            logger.warning(
                "CSP violation: %s refused %s on %s (source %s, %s)",
                fields["directive"] or "?",
                fields["blocked"] or "?",
                fields["document"] or "?",
                fields["source"] or "-",
                fields["disposition"] or "enforce",
            )
        return HttpResponse(status=204)
