"""

	Metadata:

		File: serializer.py
		Project: UrbanLens

		Author: Jess Mann
		Email: jess@urbanlens.org

		-----


		Modified By: Jess Mann

		-----

		Copyright (c) 2023 UrbanLens
"""
# Generic imports
from __future__ import annotations
# App imports
from urbanlens.dashboard.models import abstract
from urbanlens.dashboard.models.notifications.model import NotificationLog

class Serializer(abstract.Serializer):
	class Meta(abstract.Serializer.Meta):
		model = NotificationLog
		fields = [
			'id',
		]