# Generic imports
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Self, TypeVar

# Django Imports
from django.db import models as django_models

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


class PublicDashboardManager(FrontendDashboardManager.from_queryset(PublicDashboardQuerySet)):
    """
    A custom query manager. This creates QuerySets and is used in all models interacting with the app db.
    """
