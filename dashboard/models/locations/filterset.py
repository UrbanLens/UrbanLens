from django_filters import rest_framework as filters
from django_filters import CharFilter
from .model import Location

class LocationFilter(filters.FilterSet):
    categories = CharFilter(field_name='categories__name', lookup_expr='icontains')
    class Meta:
        model = Location
        fields = ['name', 'icon', 'categories', 'priority', 'last_visited', 'latitude', 'longitude']
