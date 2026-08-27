"""Shared label accessors for models carrying a ``labels`` M2M."""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.db.models import Model

from urbanlens.dashboard.models.labels.meta import KIND_CATEGORY, KIND_STATUS, KIND_TAG

if TYPE_CHECKING:
    from django.db.models.manager import Manager

    from urbanlens.dashboard.models.labels.model import Label


class LabelledModel(Model):
    """Mixin giving a model kind-specific views of its ``labels``.

    Subclasses must declare a ``labels`` many-to-many to
    :class:`~urbanlens.dashboard.models.labels.model.Label`; this mixin adds no
    fields of its own.

    The accessors filter in Python rather than issuing ``labels.filter(kind=...)``
    so that a caller's ``prefetch_related("labels")`` is actually used. Chaining a
    queryset method onto ``.all()`` builds a new queryset, which ignores the
    prefetch cache and re-queries per instance - so rendering a list of pins cost
    one query per kind per row even though the labels were already in memory.
    This matches how ``services.map_pins.payload`` builds the same three groups.

    Without a prefetch the cost is unchanged: one query to load the row's labels,
    versus one filtered query before.
    """

    class Meta:
        abstract = True

    if TYPE_CHECKING:
        #: Declared, not defined: each subclass supplies its own ``labels``
        #: many-to-many (they differ in ``related_name``). Stating it here makes
        #: the mixin's requirement explicit and type-checked, rather than leaving
        #: the accessors below reaching for an attribute mypy cannot see.
        labels: Manager[Label]

    def _labels_of_kind(self, kind: str) -> list[Label]:
        """Labels of one kind, read from the prefetch cache when there is one.

        Args:
            kind: A ``KIND_*`` value from :mod:`dashboard.models.labels.meta`.

        Returns:
            The attached labels of that kind, in the order ``labels`` yields them
            (which a caller's ``Prefetch`` may have ordered).
        """
        return [label for label in self.labels.all() if label.kind == kind]

    @property
    def categories(self) -> list[Label]:
        """Labels of kind "category" attached to this object."""
        return self._labels_of_kind(KIND_CATEGORY)

    @property
    def tags(self) -> list[Label]:
        """Labels of kind "tag" attached to this object."""
        return self._labels_of_kind(KIND_TAG)

    @property
    def statuses(self) -> list[Label]:
        """Labels of kind "status" attached to this object."""
        return self._labels_of_kind(KIND_STATUS)
