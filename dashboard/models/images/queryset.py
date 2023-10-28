from django.db.models import QuerySet, Manager
from .model import Image

class ImageQuerySet(QuerySet):
    pass

class ImageManager(Manager):
    def get_queryset(self):
        return ImageQuerySet(self.model, using=self._db)

Image.objects = ImageManager()
