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
*        Path:    /dashboard/models/notifications/model.py                                                             *
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
*        2023-12-24     By Jess Mann                                                                                   *
*                                                                                                                      *
*********************************************************************************************************************"""

# Generic imports
from __future__ import annotations
import logging
from typing import TYPE_CHECKING
# Django Imports
from django.db.models import Index
# 3rd Party Imports
from django.db.models.fields import CharField
# App Imports
from dashboard.models import abstract
from dashboard.models.notifications.meta import Status, Importance, NotificationType
from dashboard.models.notifications.queryset import Manager

if TYPE_CHECKING:
	# Imports required for type checking, but not program execution.
	pass

#
# Set up logging for this module. __name__ includes the namespace (e.g. dashboard.models.cases).
#
# We can adjust logging settings from the namespace down to the module level in UrbanLens/settings
#
logger = logging.getLogger(__name__)

class NotificationLog(abstract.Model):
	"""
	Records important notifications to check on later.
	"""
	#id = BigAutoField() # primary-key, auto-generated, can also be referred to by {self.pk} or {queryset.filter(pk=...)}
	status = CharField(max_length=17, choices=Status.choices, default=Status.UNREAD)
	importance = CharField(max_length=17, choices=Importance.choices, default=Importance.LOWEST)
	notificaiton_type = CharField(max_length=17, choices=NotificationType.choices, default=NotificationType.ERROR)
	message = CharField(max_length=50000, blank=True)

	objects = Manager()

	class Meta(abstract.Model.Meta):
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
		# Tell django where to find the table (in this schema)
		db_table = 'dashboard_notifications'
		get_latest_by = 'updated'

		indexes = [
			Index(fields=['status']),
			Index(fields=['importance']),
			Index(fields=['notificaiton_type']),
		]