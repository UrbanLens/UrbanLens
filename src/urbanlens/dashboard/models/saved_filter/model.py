"""SavedFilter model - a user's named, reusable main-map filter combination."""

from __future__ import annotations

import json
import logging

from django.db.models import CASCADE, CharField, ForeignKey, Index, IntegerField, JSONField
from django.db.models.constraints import UniqueConstraint

from urbanlens.dashboard.models import abstract

logger = logging.getLogger(__name__)


class SavedFilter(abstract.FrontendDashboardModel):
    """A profile's saved main-map filter combination.

    ``criteria`` stores a JSON-safe, normalized form of the fields
    ``SearchForm.cleaned_data`` (plus parsed ``label_groups``/custom-field
    criteria) would produce - see ``dashboard.services.filter_criteria`` for
    the (de)serialization helpers that build and replay this shape against
    ``Pin.objects.filter_by_criteria()``.
    """

    profile = ForeignKey("dashboard.Profile", on_delete=CASCADE, related_name="saved_filters")
    name = CharField(max_length=100)
    criteria = JSONField(default=dict)
    order = IntegerField(default=0)

    def __str__(self) -> str:
        return self.name

    @property
    def criteria_json(self) -> str:
        """``criteria`` as a JSON string for embedding in a ``data-*`` HTML attribute.

        Relies on Django's default template autoescaping to make the quotes
        and angle brackets this can contain attribute-safe - do not render
        with ``|safe``.
        """
        return json.dumps(self.criteria)

    class Meta(abstract.FrontendDashboardModel.Meta):
        db_table = "dashboard_saved_filters"
        ordering = ["order", "-created"]
        constraints = [UniqueConstraint(fields=["profile", "name"], name="uq_saved_filter_profile_name")]
        indexes = [Index(fields=["profile"], name="idxdb_savedfilter_profile")]
