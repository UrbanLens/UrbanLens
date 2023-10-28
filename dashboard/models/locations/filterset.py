from django_filters import rest_framework as filters
from .model import Location

class LocationFilter(filters.FilterSet):
    class Meta:
        model = Location
        fields = ['name', 'icon', 'categories', 'priority', 'last_visited', 'latitude', 'longitude']
