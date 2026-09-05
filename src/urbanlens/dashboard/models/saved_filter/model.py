"""SavedFilter model - a user's named, reusable main-map filter combination."""

from __future__ import annotations

import json
import logging

from django.db.models import CASCADE, CharField, ForeignKey, Index, IntegerField, JSONField
from django.db.models.constraints import UniqueConstraint

from urbanlens.dashboard.models import abstract
from urbanlens.dashboard.models.labels.meta import COLOR_CHOICES
from urbanlens.dashboard.models.saved_filter.queryset import SavedFilterManager
from urbanlens.dashboard.services.core.colors import clean_color

logger = logging.getLogger(__name__)


class SavedFilter(abstract.FrontendDashboardModel):
    """A profile's saved main-map filter combination.

    ``criteria`` stores a JSON-safe, normalized form of the fields
    ``SearchForm.cleaned_data`` (plus parsed ``label_groups``/custom-field
    criteria) would produce - see ``dashboard.services.search.filter_criteria`` for
    the (de)serialization helpers that build and replay this shape against
    ``Pin.objects.filter_by_criteria()``.

    Attributes:
        icon: Material Symbols icon name or emoji shown on the filter's button.
        color: Optional accent color (shared :data:`COLOR_CHOICES` palette) tinted
            behind the icon, so filters are visually distinguishable from one another.
        opacity: 0-100 alpha applied to ``color`` when tinting the button.
    """

    profile = ForeignKey("dashboard.Profile", on_delete=CASCADE, related_name="saved_filters")
    name = CharField(max_length=100)
    icon = CharField(max_length=64, blank=True, default="bookmark")
    color = CharField(max_length=20, blank=True, default="", choices=COLOR_CHOICES)
    opacity = IntegerField(default=100)
    criteria = JSONField(default=dict)
    order = IntegerField(default=0)

    def coerce_colors(self) -> None:
        """Drop `color` to its unset value unless it is a colour we can store.

        The column declares `choices`, which Django enforces in a form and not
        in the database - and the archive importer writes this straight from an
        uploaded file. The value is tinted into an inline style on the filter's
        button.
        """
        self.color = clean_color(self.color, default="")

    def save(self, *args, **kwargs) -> None:
        """Persist the filter, coercing its colour first."""
        update_fields = kwargs.get("update_fields")
        if update_fields is None or "color" in update_fields:
            self.coerce_colors()
        super().save(*args, **kwargs)

    objects = SavedFilterManager()

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
