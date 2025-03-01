"""

	Metadata:

		File: viewset.py
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
from rest_framework import filters
from UrbanLens.dashboard.models import abstract
from UrbanLens.dashboard.models.notifications.model import NotificationLog
from UrbanLens.dashboard.models.notifications.serializer import Serializer

class ViewSet(abstract.ViewSet):
	serializer_class = Serializer
	queryset = NotificationLog.objects.all()
	filter_backends = [filters.OrderingFilter]
	ordering_fields = ['id'] #['-updated', '-created', 'status', 'guid']
	ordering = ['id'] #['-updated', '-created', 'id']
