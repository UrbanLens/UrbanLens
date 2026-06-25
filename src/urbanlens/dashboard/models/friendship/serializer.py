from rest_framework import serializers

from urbanlens.dashboard.models.friendship.model import Friendship


class FriendshipSerializer(serializers.ModelSerializer):
    class Meta:
        model = Friendship
        fields = ["from_profile", "to_profile", "relationship_type", "status"]
