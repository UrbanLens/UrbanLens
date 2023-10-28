from django.db.models import QuerySet, Manager
from .model import Comment

class CommentQuerySet(QuerySet):
    pass

class CommentManager(Manager):
    def get_queryset(self):
        return CommentQuerySet(self.model, using=self._db)

Comment.objects = CommentManager()
