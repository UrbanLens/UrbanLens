"""SocialLink serializer."""

from rest_framework import serializers

from urbanlens.dashboard.models.social_link.model import SocialLink


class SocialLinkSerializer(serializers.ModelSerializer):
    class Meta:
        model = SocialLink
        fields = ["id", "platform", "handle"]
        read_only_fields = ["id"]
