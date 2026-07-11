from rest_framework import serializers

from urbanlens.dashboard.models.friendship.model import Friendship


class FriendshipSerializer(serializers.ModelSerializer):
    """Serializer for a friendship edge between two profiles."""

    class Meta:
        model = Friendship
        fields = ["id", "from_profile", "to_profile", "relationship_type", "status"]
        # from_profile is always the requesting user (set server-side), and
        # status transitions go through the accept/decline/block model
        # methods, never raw writes.
        read_only_fields = ["from_profile", "status"]
        extra_kwargs = {
            "relationship_type": {"required": False},
        }
