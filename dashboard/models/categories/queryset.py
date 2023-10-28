from django.db.models import QuerySet, Manager
from .model import Category

class CategoryQuerySet(QuerySet):
    pass

class CategoryManager(Manager):
    def get_queryset(self):
        return CategoryQuerySet(self.model, using=self._db)

Category.objects = CategoryManager()
