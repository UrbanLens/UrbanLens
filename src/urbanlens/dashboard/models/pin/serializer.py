import logging

from rest_framework import serializers

from urbanlens.dashboard.models.pin.model import Pin

logger = logging.getLogger(__name__)


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
            "name",
            "name_is_user_provided",
            "effective_name",
            "icon",
            "effective_icon",
            "categories",
            "last_visited",
            "latitude",
            "longitude",
            "effective_latitude",
            "effective_longitude",
            "is_private",
            "created",
            "updated",
            "profile",
            "tags",
            "rating",
        ]

    def create(self, validated_data):
        user = validated_data.pop("user")
        if "name_is_user_provided" not in validated_data:
            validated_data["name_is_user_provided"] = bool((validated_data.get("name") or "").strip())
        pin = Pin.objects.create(**validated_data)
        pin.user = user
        pin.save()
        try:
            from urbanlens.dashboard.services.auto_tag import AutoTagService

            AutoTagService().suggest_for_pin(pin, apply=True)
        except (RuntimeError, OSError, ValueError):
            logger.warning("Auto-tagging failed for pin %s", pin.pk, exc_info=True)
        return pin
