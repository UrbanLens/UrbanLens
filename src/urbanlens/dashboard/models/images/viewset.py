from rest_framework import viewsets

from urbanlens.dashboard.models.images.model import Image
from urbanlens.dashboard.models.images.serializer import ImageSerializer


class ImageViewSet(viewsets.ModelViewSet):
    queryset = Image.objects.all()
    serializer_class = ImageSerializer
