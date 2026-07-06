"""SocialLink model - stores one social/community link per platform per profile."""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.db.models import CASCADE, CharField, ForeignKey, Index, UniqueConstraint

from urbanlens.dashboard.models import abstract
from urbanlens.dashboard.models.social_link.queryset import Manager


class SocialLink(abstract.Model):
    """A single social media or community link belonging to a user profile.

    Storing links in a separate table (rather than as columns on Profile) means
    new platforms can be added without schema migrations - only the service-layer
    lookup tables need updating.

    The ``platform`` field is a free-form string key (e.g. ``"instagram"``,
    ``"bluesky"``).  Validation and URL construction are handled by
    :mod:`urbanlens.dashboard.services.social_links`, not by this model.
    """

    platform = CharField(max_length=30)
    handle = CharField(max_length=500)

    profile = ForeignKey(
        "dashboard.Profile",
        on_delete=CASCADE,
        related_name="social_links",
    )

    objects = Manager()

    if TYPE_CHECKING:
        profile_id: int

    def __str__(self) -> str:
        return f"{self.profile} - {self.platform}: {self.handle}"

    class Meta(abstract.Model.Meta):
        db_table = "dashboard_social_links"
        constraints = [
            UniqueConstraint(fields=["profile", "platform"], name="social_link_unique_profile_platform"),
        ]
        indexes = [
            Index(fields=["profile"], name="idxdb_soc_link_pfile"),
        ]
