"""LocationEdit - records every community edit made to a Location's wiki fields."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from django.db.models import CASCADE, SET_NULL, BooleanField, ForeignKey, Index, JSONField

from urbanlens.dashboard.models import abstract

if TYPE_CHECKING:
    from urbanlens.dashboard.models.location.model import Location
    from urbanlens.dashboard.models.profile.model import Profile

logger = logging.getLogger(__name__)


class LocationEdit(abstract.Model):
    """A single community edit applied to a Location's wiki-editable fields.

    Each edit stores the set of field changes as a JSON diff:
        {"name": {"from": "Old Name", "to": "New Name"}, ...}

    Reverts are implemented as new LocationEdit rows (so they appear in history)
    that carry the inverted diff, with ``reverted_by`` pointing at the edit being
    undone.

    Wiki-editable fields: name, description, latitude, longitude.
    Bounding-box changes are stored as WKT strings under the key "bounding_box".
    """

    location: Location = ForeignKey(
        "dashboard.Location",
        on_delete=CASCADE,
        related_name="edits",
    )
    editor: Profile | None = ForeignKey(
        "dashboard.Profile",
        on_delete=SET_NULL,
        null=True,
        blank=True,
        related_name="location_edits",
    )
    # {"field": {"from": old_val, "to": new_val}, ...}
    changes = JSONField()
    # True when this edit has been superseded by a revert.
    reverted = BooleanField(default=False)
    # The edit that reverted this one (filled in on the *target* edit when someone reverts it).
    reverted_by: LocationEdit | None = ForeignKey(
        "self",
        on_delete=SET_NULL,
        null=True,
        blank=True,
        related_name="reverts",
    )

    class Meta(abstract.Model.Meta):
        db_table = "dashboard_location_edits"
        ordering = ["-created"]
        get_latest_by = "created"
        indexes = [
            Index(fields=["location"], name="dashboard_le_location_idx"),
            Index(fields=["location", "created"], name="dashboard_le_created_idx"),
        ]
