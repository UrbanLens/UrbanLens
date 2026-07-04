from rest_framework import serializers

from urbanlens.dashboard.models.badges.serializer import BadgeSerializer
from urbanlens.dashboard.models.location.model import Location


class LocationSerializer(serializers.ModelSerializer):
    official_name = serializers.ReadOnlyField()
    categories = BadgeSerializer(many=True, read_only=True)
    tags = BadgeSerializer(many=True, read_only=True)
    statuses = BadgeSerializer(many=True, read_only=True)

    class Meta:
        model = Location
        fields = ["name", "official_name", "categories", "latitude", "longitude", "created", "updated", "tags", "statuses"]

    def create(self, validated_data):
        location = Location.objects.create(**validated_data)
        location.save()
        return location
