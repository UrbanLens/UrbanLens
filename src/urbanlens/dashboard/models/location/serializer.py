from rest_framework import serializers

from urbanlens.dashboard.models.location.model import Location


class LocationSerializer(serializers.ModelSerializer):
    official_name = serializers.ReadOnlyField()

    class Meta:
        model = Location
        fields = ["name", "official_name", "categories", "latitude", "longitude", "created", "updated", "tags"]

    def create(self, validated_data):
        location = Location.objects.create(**validated_data)
        location.save()
        return location
