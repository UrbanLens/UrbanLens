"""Link models - external website URLs attached to Pins (personal) and Wikis (shared).

Each link may carry a Wayback Machine snapshot URL, captured asynchronously
(see services.wayback_archive) so a dead or altered external page can still be
viewed as it was when the link was added.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING
from urllib.parse import urlparse

from django.db.models import CASCADE, SET_NULL, ForeignKey, Index
from django.db.models.fields import CharField, IntegerField, URLField

from urbanlens.dashboard.models import abstract
from urbanlens.dashboard.models.links.queryset import LinkManager

logger = logging.getLogger(__name__)

#: URLField's own max_length - kept generous since some CMS/tracking URLs are long.
MAX_LINK_URL_LENGTH = 2000


class _LinkBase(abstract.DashboardModel):
    """Shared fields for all link types."""

    name = CharField(max_length=255, blank=True, default="")
    url = URLField(max_length=MAX_LINK_URL_LENGTH)
    wayback_url = URLField(max_length=MAX_LINK_URL_LENGTH, blank=True, default="")
    order = IntegerField(default=0)

    objects: LinkManager = LinkManager()  # pyright: ignore[reportIncompatibleVariableOverride]

    class Meta(abstract.DashboardModel.Meta):
        abstract = True
        ordering = ["order", "id"]

    def save(self, *args, **kwargs) -> None:
        """Sanitize ``name`` to a strict character set before persisting it.

        Single enforcement point regardless of write path (manual add, KMZ
        import link extraction, ...) - mirrors ``_AliasBase.save()``.
        """
        from urbanlens.dashboard.services.locations.naming import sanitize_name

        update_fields = kwargs.get("update_fields")
        if update_fields is None or "name" in update_fields:
            self.name = sanitize_name(self.name) or ""
        super().save(*args, **kwargs)

    def __str__(self) -> str:
        return self.display_name

    @property
    def display_name(self) -> str:
        """The name to show in the UI: the user-given name, or the URL's bare domain."""
        if self.name:
            return self.name
        return urlparse(self.url).netloc or self.url


class PinLink(_LinkBase):
    """An external URL attached to a Pin, visible only to the pin's owner."""

    pin = ForeignKey(
        "dashboard.Pin",
        on_delete=CASCADE,
        related_name="links",
    )

    if TYPE_CHECKING:
        pin_id: int

    class Meta(_LinkBase.Meta):
        db_table = "dashboard_pin_links"
        indexes = [
            Index(fields=["pin"], name="idxdb_plink_pin"),
        ]


class WikiLink(_LinkBase):
    """An external URL attached to a Wiki, visible to all users who have its place pinned."""

    wiki = ForeignKey(
        "dashboard.Wiki",
        on_delete=CASCADE,
        related_name="links",
    )
    created_by = ForeignKey(
        "dashboard.Profile",
        on_delete=SET_NULL,
        null=True,
        blank=True,
        related_name="wiki_links_created",
    )

    if TYPE_CHECKING:
        wiki_id: int
        created_by_id: int | None

    class Meta(_LinkBase.Meta):
        db_table = "dashboard_wiki_links"
        indexes = [
            Index(fields=["wiki"], name="idxdb_wlink_wiki"),
        ]
