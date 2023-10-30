import django_filters
from .model import Category

class CategoryFilter(django_filters.FilterSet):
    class Meta:
        model = Category
        fields = ['name']
