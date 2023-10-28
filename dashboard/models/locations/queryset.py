"""
    Metadata:

        File: queryset.py
        Project: UrbanLens
        Author: Jess Mann
        Email: jess@manlyphotos.com

        -----

        Copyright (c) 2023 UrbanLens
"""
# Generic imports
from __future__ import annotations
from typing import TYPE_CHECKING
import logging
# Django Imports
from djangofoundry import models
# App Imports
from dashboard.models import abstract

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

class QuerySet(abstract.QuerySet):
    '''
    A custom queryset. All models below will use this for interacting with results from the db.
    '''

class Manager(abstract.Manager.from_queryset(QuerySet)):
    '''
    A custom query manager. This creates QuerySets and is used in all models interacting with the app db.
    '''
