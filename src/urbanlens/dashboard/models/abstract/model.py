"""*********************************************************************************************************************
*                                                                                                                      *
*                                                                                                                      *
*                                                                                                                      *
*                                                                                                                      *
* -------------------------------------------------------------------------------------------------------------------- *
*                                                                                                                      *
*    METADATA:                                                                                                         *
*                                                                                                                      *
*        File:    model.py                                                                                             *
*        Path:    /dashboard/models/abstract/model.py                                                                  *
*        Project: urbanlens                                                                                            *
*        Version: 0.0.2                                                                                                *
*        Created: 2023-12-24                                                                                           *
*        Author:  Jess Mann                                                                                            *
*        Email:   jess@urbanlens.org                                                                                 *
*        Copyright (c) 2025 Jess Mann                                                                                  *
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
# Django Imports
from django.db import models as django_models
# Lib Imports
# App Imports
from urbanlens.dashboard.models.abstract.queryset import Manager

#
# Set up logging for this module. __name__ includes the namespace (e.g. dashboard.models.cases).
#
# We can adjust logging settings from the namespace down to the module level in UrbanLens/settings
#
logger = logging.getLogger(__name__)

class Model(django_models.Model):
	'''
	A base model that all other models in this app inherit from.
	'''
	created = django_models.DateTimeField(auto_now_add=True)
	updated = django_models.DateTimeField(auto_now=True)
	objects: Manager = Manager()
	postgres: Manager = Manager()

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
		app_label = 'dashboard'