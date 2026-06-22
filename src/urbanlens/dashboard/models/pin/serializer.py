from rest_framework import serializers

from urbanlens.dashboard.models.pin.model import Pin


class PinSerializer(serializers.ModelSerializer):
    """Serializer for Pin - exposes user-specific fields only.

    Address, place name, and canonical coordinates are NOT included here because
    they live on the related Location.  If API consumers need place-level data,
    nest a LocationSerializer or add read-only source= fields pointing through
    the location FK (e.g. source="location.address").

    effective_* fields are read-only - they resolve overrides against the linked
    Location and should be preferred by the frontend over the raw nullable fields.
    """

    effective_name = serializers.ReadOnlyField()
    effective_icon = serializers.ReadOnlyField()
    effective_latitude = serializers.ReadOnlyField()
    effective_longitude = serializers.ReadOnlyField()

    class Meta:
        model = Pin
        fields = [
            "id",
            "nickname",
            "effective_name",
            "icon",
            "effective_icon",
            "categories",
            "last_visited",
            "latitude",
            "longitude",
            "effective_latitude",
            "effective_longitude",
            "created",
            "updated",
            "profile",
            "tags",
            "rating",
        ]

    def create(self, validated_data):
        user = validated_data.pop("user")
        pin = Pin.objects.create(**validated_data)
        pin.user = user
        pin.save()
        return pin
