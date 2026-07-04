from rest_framework import serializers

from urbanlens.dashboard.models.reviews.model import Review


class ReviewSerializer(serializers.ModelSerializer):
    class Meta:
        model = Review
        fields = ["id", "user", "pin", "rating", "review"]

    def create(self, validated_data):
        user = validated_data.pop("user")
        review = Review.objects.create(**validated_data)
        review.user = user
        review.save()
        return review
