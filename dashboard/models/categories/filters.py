from django_filters import rest_framework as filters
from .model import Category

class CategoryFilter(filters.FilterSet):
    class Meta:
        model = Category
        fields = ['name']
