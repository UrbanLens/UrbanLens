from rest_framework import serializers

from urbanlens.dashboard.models.comments.model import Comment


class CommentSerializer(serializers.ModelSerializer):
    class Meta:
        model = Comment
        fields = ["text", "pin", "profile", "created", "updated"]
