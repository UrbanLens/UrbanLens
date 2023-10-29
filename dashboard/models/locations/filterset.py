from django_filters import rest_framework as filters
from django_filters import CharFilter, BooleanFilter, NumberFilter, MethodFilter
from .model import Location

class LocationFilter(filters.FilterSet):
    categories = CharFilter(method='by_category')
    never_visited = MethodFilter(method='never_visited')
    not_visited_this_year = MethodFilter(method='not_visited_this_year')
    by_priority = NumberFilter(method='by_priority')
    by_latitude = NumberFilter(method='by_latitude')
    by_longitude = NumberFilter(method='by_longitude')
    by_name = CharFilter(method='by_name')
    by_created_year = NumberFilter(method='by_created_year')
    by_updated_year = NumberFilter(method='by_updated_year')

    class Meta:
        model = Location
        fields = ['name', 'icon', 'categories', 'priority', 'last_visited', 'latitude', 'longitude', 'never_visited', 'not_visited_this_year', 'by_priority', 'by_latitude', 'by_longitude', 'by_name', 'by_created_year', 'by_updated_year']
