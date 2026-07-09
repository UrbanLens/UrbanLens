from rest_framework import serializers

from urbanlens.dashboard.models.reviews.model import Review


class ReviewSerializer(serializers.ModelSerializer):
    class Meta:
        model = Review
        fields = ["id", "profile", "pin", "rating", "review"]

    def create(self, validated_data):
        return Review.objects.create(**validated_data)
