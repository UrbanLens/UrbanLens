"""*********************************************************************************************************************
*                                                                                                                      *
*                                                                                                                      *
*                                                                                                                      *
*                                                                                                                      *
* -------------------------------------------------------------------------------------------------------------------- *
*                                                                                                                      *
*    METADATA:                                                                                                         *
*                                                                                                                      *
*        File:    queryset.py                                                                                          *
*        Path:    /dashboard/models/abstract/queryset.py                                                               *
*        Project: urbanlens                                                                                            *
*        Version: 1.0.0                                                                                                *
*        Created: 2023-12-24                                                                                           *
*        Author:  Jess Mann                                                                                            *
*        Email:   jess@urbanlens.org                                                                                 *
*        Copyright (c) 2023 - 2024 Urban Lens                                                                          *
*                                                                                                                      *
* -------------------------------------------------------------------------------------------------------------------- *
*                                                                                                                      *
*    LAST MODIFIED:                                                                                                    *
*                                                                                                                      *
*        2024-01-01     By Jess Mann                                                                                   *
*                                                                                                                      *
*********************************************************************************************************************"""

# Generic imports
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from django.db import models as django_models

# Django Imports
# Lib Imports
# App Imports

# Typechecking imports

#
# Set up logging for this module. __name__ includes the namespace (e.g. dashboard.models.cases).
#
# We can adjust logging settings from the namespace down to the module level in UrbanLens/settings
#
logger = logging.getLogger(__name__)

'''
class QuerySet(models.QuerySet):
    """
    A custom queryset. All models below will use this for interacting with results from the db.
    """

class Manager(models.PostgresManager.from_queryset(QuerySet)):
    """
    A custom query manager. This creates QuerySets and is used in all models interacting with the app db.
    """
'''


class QuerySet(django_models.QuerySet):
    """
    A custom queryset. All models below will use this for interacting with results from the db.
    """


class Manager(django_models.Manager.from_queryset(QuerySet)):
    """
    A custom query manager. This creates QuerySets and is used in all models interacting with the app db.
    """
