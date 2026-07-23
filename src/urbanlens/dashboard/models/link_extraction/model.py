"""LinkExtraction - one AI run over one external link attached to a pin.

Each row records a single "process this link with AI" request end to end:
who asked, which pin and url, how the run ended, and the per-field results
(what the AI proposed, what was actually applied, and why anything was
skipped). The rows double as the per-user daily-limit ledger and as the
data source for the unlinked review page.

The AI's output never touches a Pin directly from here - values flow
through the deterministic field registry in
``services.ai.link_extraction`` (parse + sanitize + allowlisted apply),
and this model only stores the already-sanitized display record of what
happened.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from django.db.models import CASCADE, CharField, ForeignKey, Index, JSONField, TextChoices, TextField, URLField

from urbanlens.dashboard.models import abstract
from urbanlens.dashboard.models.link_extraction.queryset import LinkExtractionManager

logger = logging.getLogger(__name__)

#: Matches PinLink/WikiLink's cap so any attached link can be submitted verbatim.
MAX_EXTRACTION_URL_LENGTH = 2000


class LinkExtractionStatus(TextChoices):
    """Lifecycle of one extraction run."""

    PENDING = "pending", "Queued"
    RUNNING = "running", "Processing"
    SUCCESS = "success", "Complete"
    EMPTY = "empty", "Nothing found"
    FAILED = "failed", "Failed"


class LinkExtraction(abstract.DashboardModel):
    """One AI processing run over one external link for one of a user's pins.

    Attributes:
        profile: The requesting user; runs are deleted with the account.
        pin: The pin the link belongs to; runs are deleted with the pin
            (a result record with no pin to apply to has no remaining value).
        url: The external page that was processed.
        status: Where the run is in its lifecycle (:class:`LinkExtractionStatus`).
        error: User-facing failure summary when ``status`` is FAILED.
        results: List of per-field outcome dicts, each
            ``{"key", "label", "value", "applied", "note"}`` - ``value`` is the
            sanitized display string of what the AI proposed, ``applied`` is
            whether it was written to the pin, and ``note`` explains a skip
            (e.g. the field already had a value). Only registry-allowlisted
            keys ever appear here.
    """

    profile = ForeignKey("dashboard.Profile", on_delete=CASCADE, related_name="link_extractions")
    pin = ForeignKey("dashboard.Pin", on_delete=CASCADE, related_name="link_extractions")
    url = URLField(max_length=MAX_EXTRACTION_URL_LENGTH)
    status = CharField(max_length=10, choices=LinkExtractionStatus.choices, default=LinkExtractionStatus.PENDING)
    error = TextField(blank=True, default="")
    results = JSONField(default=list, blank=True)

    objects: LinkExtractionManager = LinkExtractionManager()

    if TYPE_CHECKING:
        profile_id: int
        pin_id: int

    class Meta(abstract.DashboardModel.Meta):
        db_table = "dashboard_link_extractions"
        ordering = ["-created"]
        indexes = [
            # Backs both the review page (profile, newest first) and the
            # daily-limit count (profile, created >= midnight).
            Index(fields=["profile", "created"], name="idxdb_linkext_profile_created"),
        ]

    def __str__(self) -> str:
        return f"LinkExtraction({self.pk}, pin={self.pin_id}, {self.status})"

    @property
    def applied_count(self) -> int:
        """How many extracted fields were actually written to the pin."""
        return sum(1 for row in self.results_rows if row.get("applied"))

    @property
    def results_rows(self) -> list[dict[str, Any]]:
        """The results list, defensively coerced for template consumption."""
        return self.results if isinstance(self.results, list) else []
