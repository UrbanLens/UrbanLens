# Generic imports
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

# Django Imports
# App Imports
from urbanlens.dashboard.models import abstract

logger = logging.getLogger(__name__)


class QuerySet(abstract.QuerySet):
    """
    A custom queryset. All models below will use this for interacting with results from the db.
    """


class Manager(abstract.Manager.from_queryset(QuerySet)):
    """
    A custom query manager. This creates QuerySets and is used in all models interacting with the app db.
    """
