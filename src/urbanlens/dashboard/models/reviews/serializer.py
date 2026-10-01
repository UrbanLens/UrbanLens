from rest_framework import serializers

from urbanlens.dashboard.models.reviews.model import Review


class ReviewSerializer(serializers.ModelSerializer):
    """Serializer for Review.

    ``profile`` and ``pin`` are set server-side - never accepted from the client.
    """

    profile = serializers.PrimaryKeyRelatedField(read_only=True)
    pin = serializers.PrimaryKeyRelatedField(read_only=True)

    class Meta:
        model = Review
        fields = ["id", "profile", "pin", "rating"]

    def create(self, validated_data):
        return Review.objects.create(**validated_data)
