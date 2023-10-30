import django_filters
from .model import Comment

class CommentFilter(django_filters.FilterSet):
    class Meta:
        model = Comment
        fields = ['text', 'location', 'profile']
