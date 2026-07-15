# Generic imports
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Self, TypeVar
import uuid as uuid_lib

# Django Imports
from django.db import models as django_models
from django.db.models import Q

# Lib Imports
# App Imports

logger = logging.getLogger(__name__)

_ModelT = TypeVar("_ModelT", bound=django_models.Model)


class DashboardQuerySet(django_models.QuerySet[_ModelT]):
    """
    A custom queryset. All models below will use this for interacting with results from the db.

    Generic over the concrete model type so that subclasses can parameterize it (e.g.
    ``abstract.QuerySet["Friendship"]``) and get correctly-typed ``.get()``/``.first()``/etc. results.
    """


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

        Every public URL for one of these models builds its identifier as
        ``obj.slug or str(obj.uuid)`` - the uuid fallback fires whenever a row's
        slug hasn't been minted (e.g. a legacy row predating auto-slug
        generation, or one saved via a path that bypassed it). An endpoint that
        only ever looks up ``slug=value`` 404s for exactly those rows even
        though the value it received is a perfectly valid identifier for them.

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
