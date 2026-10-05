"""Link models - external website URLs attached to Pins (personal) and Wikis (shared).

Each link may carry a Wayback Machine snapshot URL, captured asynchronously
(see services.links.wayback_archive) so a dead or altered external page can
still be viewed as it was when the link was added.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING
from urllib.parse import urlparse

from django.contrib.postgres.indexes import HashIndex
from django.db.models import CASCADE, SET_NULL, F, ForeignKey, Index, Q, TextChoices, UniqueConstraint
from django.db.models.fields import CharField, DateTimeField, IntegerField, PositiveSmallIntegerField, URLField
from django.db.models.functions import MD5

from urbanlens.dashboard.models import abstract
from urbanlens.dashboard.models.links.queryset import LinkManager

logger = logging.getLogger(__name__)

#: URLField's own max_length - kept generous since some CMS/tracking URLs are long.
MAX_LINK_URL_LENGTH = 2000


class AutoLinkSource(TextChoices):
    """The provider that added a link automatically. A link a person added has none."""

    OPENSTREETMAP = "openstreetmap", "OpenStreetMap"
    EPA_ECHO = "epa_echo", "EPA ECHO"
    WIKIPEDIA = "wikipedia", "Wikipedia"
    NATIONAL_REGISTER = "nrhp", "National Register of Historic Places"


class _LinkBase(abstract.DashboardModel):
    """Shared fields for all link types.

    Attributes:
        auto_source: The :class:`AutoLinkSource` that added this link, or ``""`` when a person did.
        wayback_attempts: Failed attempts to archive ``url``, shared by every link waiting on it.
        wayback_retry_at: When ``url`` may next be asked about; None before the first attempt and once given up.
    """

    name = CharField(max_length=255, blank=True, default="")
    url = URLField(max_length=MAX_LINK_URL_LENGTH)
    wayback_url = URLField(max_length=MAX_LINK_URL_LENGTH, blank=True, default="")
    order = IntegerField(default=0)
    auto_source = CharField(max_length=32, choices=AutoLinkSource.choices, blank=True, default="", db_default="")
    wayback_attempts = PositiveSmallIntegerField(default=0, db_default=0)
    wayback_retry_at = DateTimeField(null=True, blank=True)

    objects = LinkManager()

    class Meta(abstract.DashboardModel.Meta):
        abstract = True
        ordering = ["order", "id"]

    def save(self, *args, **kwargs) -> None:
        """Sanitize ``name`` and refuse a URL that is not an http(s) link.

        Raises:
            InvalidLinkUrlError: ``url`` or ``wayback_url`` is not an http(s) link; both are rendered as ``href``.
        """
        from urbanlens.dashboard.services.locations.naming import sanitize_name
        from urbanlens.dashboard.services.security.link_urls import clean_link_url

        update_fields = kwargs.get("update_fields")
        if update_fields is None or "name" in update_fields:
            self.name = sanitize_name(self.name) or ""
        if update_fields is None or "url" in update_fields:
            self.url = clean_link_url(self.url, max_length=MAX_LINK_URL_LENGTH)
        if self.wayback_url and (update_fields is None or "wayback_url" in update_fields):
            self.wayback_url = clean_link_url(self.wayback_url, max_length=MAX_LINK_URL_LENGTH)
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
        constraints = [
            UniqueConstraint(F("pin"), MD5("url"), name="db_plink_pin_url_unique"),
        ]
        # The Wayback archive finds every link naming a URL (services.links.wayback_archive); a URL can outgrow a btree entry.
        indexes = [
            HashIndex(fields=["url"], name="db_plink_url_hash"),
            Index(fields=["wayback_retry_at"], condition=Q(wayback_url=""), name="db_plink_wayback_due"),
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
        constraints = [
            # Hashed for the same reason as PinLink's - see the note there.
            UniqueConstraint(F("wiki"), MD5("url"), name="db_wlink_wiki_url_unique"),
        ]
        indexes = [
            HashIndex(fields=["url"], name="db_wlink_url_hash"),
            Index(fields=["wayback_retry_at"], condition=Q(wayback_url=""), name="db_wlink_wayback_due"),
        ]
