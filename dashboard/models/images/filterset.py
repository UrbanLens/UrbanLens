from django_filters import rest_framework as filters
from .model import Image

class ImageFilter(filters.FilterSet):
    class Meta:
        model = Image
        fields = ['image', 'location']
