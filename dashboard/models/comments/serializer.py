from rest_framework import serializers
from .model import Comment

class CommentSerializer(serializers.ModelSerializer):
    class Meta:
        model = Comment
        fields = ['text', 'location', 'profile', 'created', 'updated']
