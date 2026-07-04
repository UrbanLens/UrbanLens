# Generic imports
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

# Django Imports
from django.db import models as django_models

# Lib Imports
# App Imports
from urbanlens.dashboard.models.abstract.queryset import Manager

logger = logging.getLogger(__name__)


class Model(django_models.Model):
    """
    A base model that all other models in this app inherit from.
    """

    created = django_models.DateTimeField(auto_now_add=True)
    updated = django_models.DateTimeField(auto_now=True)
    objects: Manager = Manager()
    postgres: Manager = Manager()

    if TYPE_CHECKING:
        id: int

    class Meta:
        """
        Metadata about this model (such as the table name)

        Attributes:
            db_table (str):
                The name of the table in the DB
            unique_together (list of str):
                A list of attributes which form unique keys
            indexes (list of Index):
                A list of indexes to create on the table

        """

        abstract = True
        app_label = "dashboard"
