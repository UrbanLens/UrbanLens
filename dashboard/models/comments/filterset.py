from django_filters import rest_framework as filters
from .model import Comment

class CommentFilter(filters.FilterSet):
    class Meta:
        model = Comment
        fields = ['text', 'location', 'profile']
