import django_filters
from .model import Image

class ImageFilter(django_filters.FilterSet):
    class Meta:
        model = Image
        fields = ['image', 'location']
