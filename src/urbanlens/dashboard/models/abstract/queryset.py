# Generic imports
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Self, TypeVar
import uuid as uuid_lib

# Django Imports
from django.db import models as django_models
from django.db.models import Q

# Lib Imports
# App Imports
from urbanlens.core.semijoin import crosses_many, current_scope, probe_relation, probe_scope, probe_statement

if TYPE_CHECKING:
    from collections.abc import Iterable

logger = logging.getLogger(__name__)

_ModelT = TypeVar("_ModelT", bound=django_models.Model)


class DashboardQuerySet(django_models.QuerySet[_ModelT]):
    """
    A custom queryset. All models below will use this for interacting with results from the db.

    Generic over the concrete model type so that subclasses can parameterize it (e.g.
    ``abstract.QuerySet["Friendship"]``) and get correctly-typed ``.get()``/``.first()``/etc. results.
    """

    def after_bulk_write(self) -> None:
        """Called once after a bulk write that changed rows. Does nothing unless a subclass says so.

        ``update()`` sends no ``post_save``, and ``bulk_update`` is implemented as one, so anything
        derived from these rows and invalidated by a signal never hears about either. A subclass
        whose rows something caches overrides this rather than trusting the receivers to see it.
        """

    def update(self, **kwargs: Any) -> int:
        """Apply the update, then tell the subclass if it changed anything.

        Args:
            **kwargs: The fields to write, as ``QuerySet.update`` takes them.

        Returns:
            How many rows were updated.
        """
        updated = super().update(**kwargs)
        if updated:
            self.after_bulk_write()
        return updated

    def match_ids(self, limit: int | None = None) -> list[Any]:
        """The primary keys this queryset matches, read without any of the joins that hydrate a row.

        The first half of match-then-fetch: run the predicate against ids alone, then fetch those
        rows by primary key with whatever ``select_related``/``prefetch_related`` the caller wants.
        Carrying the hydration through the matching query costs planning time proportional to the
        relations it names, paid per call - 161.6 ms of it against 13.9 ms of execution on the map
        autocomplete's keystroke query at capacity scale (docs/archive/PROBLEMS-ARCHIVE.md, P100).

        Args:
            limit: Stop after this many ids; None for all of them.

        Returns:
            The matching primary keys, ordered by primary key so the fetch half can be too.
        """
        ids = self.order_by("pk").values_list("pk", flat=True).distinct()
        return list(ids[:limit] if limit is not None else ids)

    def by_ids(self, ids: Iterable[Any]) -> Self:
        """The second half of match-then-fetch: these rows, by primary key.

        Args:
            ids: Primary keys, typically from :meth:`match_ids`.

        Returns:
            This queryset restricted to them."""
        return self.filter(pk__anyof=list(ids))

    def bounded_by(self, path: str, queryset: DashboardQuerySet[Any]) -> Self:
        """This queryset restricted to rows whose *path* points at one of *queryset*'s rows.

        The difference from ``filter(path__in=queryset)`` is that the bound is resolved to a
        concrete list first, so the planner cannot decide to drive the query from the far side of
        the relation - which is what turns "the comments on trips I belong to" into a scan of every
        comment on the site: 31.44 ms reading 50,000 rows it then discards, against 0.16 ms reading
        none. See :mod:`~urbanlens.core.semijoin`.

        Args:
            path: The relation on this model to bound, e.g. ``"trip"``.
            queryset: The already access-scoped rows it must point at.

        Returns:
            This queryset, bounded."""
        return self.filter(**{f"{path}__pk__anyof": queryset.match_ids()})

    def own_pks(self) -> list[Any]:
        """The primary keys this queryset selects, fetched once per enclosing probe scope.

        Args:
            None.

        Returns:
            The primary keys, in whatever order the database returned them."""
        scope = current_scope()
        if scope is None:
            return list(self.values_list("pk", flat=True))
        return scope.pks_of(self)

    def semijoin(self, path: str, condition: Q) -> Q:
        """*condition* as a semi-join, when *path* crosses a to-many relation.

        A row matching through several related rows stays one row without ``DISTINCT``, and the
        statement never joins the relation: planning a join across several of them cost more than
        running it. The predicate is asked of the crossing's own table where its shape allows
        (:func:`~urbanlens.core.semijoin.probe_relation`) and as a bounded join from this model
        where it does not (:func:`~urbanlens.core.semijoin.probe_statement`); either way the answer
        reaches the caller as a concrete list of primary keys, which is load-bearing.

        Args:
            path: The ORM path *condition* filters through.
            condition: The predicate, written against this queryset's model.

        Returns:
            A Q usable in ``filter()`` on this queryset's model."""
        if not crosses_many(self.model, path):
            return condition
        # The scope is entered here rather than left to the caller because the setting it holds is
        # what keeps the probe on its index; a caller that forgets it gets a correct answer read
        # the wrong way, which only a rows-read measurement notices. Reentrant, so a caller who
        # does hold one still pays for it once and keeps the pk memo across its own loop.
        with probe_scope():
            outer_pks = self.own_pks()
            if not outer_pks:
                return Q(pk__in=[])
            matched = probe_relation(self.model, path, condition, outer_pks)
            if matched is None:
                matched = probe_statement(self.model, condition, outer_pks)
            return Q(pk__in=matched)


class DashboardManager(django_models.Manager.from_queryset(DashboardQuerySet)):
    """
    A custom query manager. This creates QuerySets and is used in all models interacting with the app db.
    """


class FrontendDashboardQuerySet(DashboardQuerySet[_ModelT]):
    """
    A custom queryset. All models below will use this for interacting with results from the db.
    """

    def uuid(self, uuid: str) -> Self:
        return self.filter(uuid=uuid)


class FrontendDashboardManager(DashboardManager.from_queryset(FrontendDashboardQuerySet)):
    """
    A custom query manager. This creates QuerySets and is used in all models interacting with the app db.
    """


class PublicDashboardQuerySet(FrontendDashboardQuerySet[_ModelT]):
    """
    A custom queryset. All models below will use this for interacting with results from the db.
    """

    def slug_or_uuid(self, value: str) -> Self:
        """Return the row matching this slug, or this uuid if it was sent instead.
        Every public URL for one of these models builds its identifier as ``obj.slug or str(obj.uuid)`` - the uuid fallback fires whenever a row's slug hasn't been minted (e.g. a legacy row predating auto-slug generation, or one saved via a path that bypassed it).

        Args:
            value: The slug or uuid string taken from a URL path segment.

        Returns:
            Queryset filtered to the matching row (0 or 1 results).
        """
        query = Q(slug=value)
        try:
            uuid_lib.UUID(value)
        except (ValueError, TypeError, AttributeError):
            pass
        else:
            query |= Q(uuid=value)
        return self.filter(query)


class PublicDashboardManager(FrontendDashboardManager.from_queryset(PublicDashboardQuerySet)):
    """
    A custom query manager. This creates QuerySets and is used in all models interacting with the app db.
    """
