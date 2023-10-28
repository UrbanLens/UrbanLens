from django_filters import rest_framework as filters
from .model import Profile

class ProfileFilter(filters.FilterSet):
    class Meta:
        model = Profile
        fields = ['user', 'icon', 'categories', 'priority', 'last_visited']
