from __future__ import annotations

import django_filters

from urbanlens.dashboard.models.comments.model import Comment


class CommentFilter(django_filters.FilterSet):
    class Meta:
        model = Comment
        fields = ["text", "pin", "profile"]
