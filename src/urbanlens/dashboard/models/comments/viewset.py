from __future__ import annotations

from rest_framework import viewsets

from urbanlens.dashboard.models.comments.model import Comment
from urbanlens.dashboard.models.comments.serializer import CommentSerializer


class CommentViewSet(viewsets.ModelViewSet):
    queryset = Comment.objects.all()
    serializer_class = CommentSerializer
