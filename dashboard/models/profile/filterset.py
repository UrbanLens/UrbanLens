import django_filters
from .model import Profile

class ProfileFilter(django_filters.FilterSet):
    class Meta:
        model = Profile
        fields = ['user', 'icon', 'categories', 'priority', 'last_visited']
