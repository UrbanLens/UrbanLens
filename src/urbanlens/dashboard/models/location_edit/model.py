"""LocationEdit model - community edit history for Location wiki fields."""

from __future__ import annotations

import logging

from django.db.models import CASCADE, SET_NULL, BooleanField, ForeignKey, Index, JSONField

from urbanlens.dashboard.models import abstract
from urbanlens.dashboard.models.location_edit.queryset import LocationEditManager

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

    location = ForeignKey(
        "dashboard.Location",
        on_delete=CASCADE,
        related_name="edits",
    )
    editor = ForeignKey(
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
    reverted_by = ForeignKey(
        "self",
        on_delete=SET_NULL,
        null=True,
        blank=True,
        related_name="reverts",
    )

    objects = LocationEditManager()

    class Meta(abstract.Model.Meta):
        db_table = "dashboard_location_edits"
        ordering = ["-created"]
        get_latest_by = "created"
        indexes = [
            Index(fields=["location"], name="idxdb_le_location"),
            Index(fields=["location", "created"], name="idxdb_le_created"),
        ]
