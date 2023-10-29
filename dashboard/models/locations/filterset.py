from django_filters import rest_framework as filters
from django_filters import CharFilter, BooleanFilter
from .model import Location

class LocationFilter(filters.FilterSet):
    categories = CharFilter(field_name='categories__name', lookup_expr='icontains')
    never_visited = BooleanFilter(field_name='last_visited', lookup_expr='isnull')
    class Meta:
        model = Location
        fields = ['name', 'icon', 'categories', 'priority', 'last_visited', 'latitude', 'longitude', 'never_visited']
