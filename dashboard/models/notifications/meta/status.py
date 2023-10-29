"""

	Metadata:

		File: status.py
		Project: UrbanLens

		Author: Jess Mann
		Email: jess@manlyphotos.com

		-----


		Modified By: Jess Mann

		-----

		Copyright (c) 2023 UrbanLens
"""
# Generic imports
from __future__ import annotations
from django.utils.translation import gettext as _
from djangofoundry.models.choices import TextChoices

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