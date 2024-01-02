"""*********************************************************************************************************************
*                                                                                                                      *
*                                                                                                                      *
*                                                                                                                      *
*                                                                                                                      *
* -------------------------------------------------------------------------------------------------------------------- *
*                                                                                                                      *
*    METADATA:                                                                                                         *
*                                                                                                                      *
*        File:    status.py                                                                                            *
*        Path:    /dashboard/models/notifications/meta/status.py                                                       *
*        Project: urbanlens                                                                                            *
*        Version: 1.0.0                                                                                                *
*        Created: 2023-12-24                                                                                           *
*        Author:  Jess Mann                                                                                            *
*        Email:   jess@manlyphotos.com                                                                                 *
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
from django.utils.translation import gettext as _
from dashboard.models.abstract.choices import TextChoices

class Status(TextChoices):
	"""
	Choices used for recording the status of a notification.

	This is used as a class, and never instantiated.

	Examples:
		>>> if foo.status == Status.VALIDATED:
		>>> ...

		>>> if fo.status in Status.ready_statuses:
		>>> ...

		>>> def sample( status : Status ):
		>>> ...
		>>> sample(Status.READY) # param is str("ready")
	"""
	UNREAD 				= 'read', 				_('Notifcation is unread: has not been seen.')
	READ			 	= 'unread',			 _('Notifcaiton has been seen.')
	DISMISSED		   = 'dismissed',		  _('Notification was dismissed.')