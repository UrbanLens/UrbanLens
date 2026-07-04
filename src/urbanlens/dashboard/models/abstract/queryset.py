# Generic imports
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, TypeVar

# Django Imports
from django.db import models as django_models

# Lib Imports
# App Imports

logger = logging.getLogger(__name__)

_ModelT = TypeVar("_ModelT", bound=django_models.Model)


class QuerySet(django_models.QuerySet[_ModelT]):
    """
    A custom queryset. All models below will use this for interacting with results from the db.

    Generic over the concrete model type so that subclasses can parameterize it (e.g.
    ``abstract.QuerySet["Friendship"]``) and get correctly-typed ``.get()``/``.first()``/etc. results.
    """


class Manager(django_models.Manager.from_queryset(QuerySet)):
    """
    A custom query manager. This creates QuerySets and is used in all models interacting with the app db.
    """
