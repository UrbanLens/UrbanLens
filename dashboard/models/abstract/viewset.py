"""*********************************************************************************************************************
*                                                                                                                      *
*                                                                                                                      *
*                                                                                                                      *
*                                                                                                                      *
* -------------------------------------------------------------------------------------------------------------------- *
*                                                                                                                      *
*    METADATA:                                                                                                         *
*                                                                                                                      *
*        File:    viewset.py                                                                                           *
*        Path:    /dashboard/models/abstract/viewset.py                                                                *
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
from typing import TYPE_CHECKING
# Django Imports
# Third party imports
from rest_framework.filters import OrderingFilter
from rest_framework.viewsets import ReadOnlyModelViewSet
# App imports
from djangofoundry.mixins import HasParams
from dashboard.models.abstract.serializer import Serializer

if TYPE_CHECKING:
	from dashboard.models.abstract.queryset import QuerySet


class ViewSet(HasParams, ReadOnlyModelViewSet):
	"""
	An abstract viewset class that provides a default implementation for the get_queryset method, and allows for filtering and ordering of the queryset.
	"""
	serializer_class = Serializer
	filter_backends = [OrderingFilter]
	filterset_fields : list[str] = []
	ordering_fields = ['__all__']
	ordering = ['id']

	def apply_filters(self, queryset : QuerySet) -> QuerySet:
		"""
		Apply filters to the queryset. This is applied automatically in the get_queryset method using the filterset_fields attribute and request params.

		Args:
			queryset (QuerySet): The queryset to apply filters to.

		Returns:
			QuerySet: The filtered queryset.

		Example:
			>>> queryset = self.apply_filters(queryset)
			<QuerySet [<Case: Case object (1)>, <Case: Case object (2)>]>
		"""
		# Allow all filters in filterset_fields to be applied to the queryset
		for filter_field in self.filterset_fields:
			# Check if it exists without an operation
			filter_value = self.request.GET.get(filter_field)
			if filter_value is not None:
				# Apply the filter to the queryset, one at a time
				queryset = queryset.filter(**{f'{filter_field}__exact': filter_value})
				break

			# The supported operations to perform for each filter
			operations = [ 'gt', 'lt', 'gte', 'lte', 'exact', 'startswith', 'istartswith', 'endswith', 'iendswith', 'isnull' ]
			# Check if "filter_field" or "filter_field__comparison" is in the request params
			for operation in operations:
				# Get the value of the filter from the request params, if it exists.
				filter_value = self.request.GET.get(f'{filter_field}__{operation}')
				if filter_value is not None:
					# Apply the filter to the queryset, one at a time
					queryset = queryset.filter(**{f'{filter_field}__{operation}': filter_value})

		# Return the filtered queryset
		return queryset

	def get_queryset(self) -> QuerySet:
		# Get the queryset from the parent class
		queryset = super().get_queryset()
		# Apply filters to the queryset automatically
		return self.apply_filters(queryset)
