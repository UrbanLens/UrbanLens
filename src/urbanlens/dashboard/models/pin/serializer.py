import logging

from rest_framework import serializers

from urbanlens.dashboard.models.badges.serializer import BadgeSerializer
from urbanlens.dashboard.models.pin.model import Pin

logger = logging.getLogger(__name__)


class PinSerializer(serializers.ModelSerializer):
    """Serializer for Pin - exposes user-specific fields only.

    Canonical coordinates are read from the related Location (``pin.location``).
    ``latitude`` and ``longitude`` are accepted on write for API compatibility
    (e.g. duplicate-pin checks) but are not stored on Pin itself.

    Address and place name are not included here; nest a LocationSerializer or add
    read-only ``source="location.*"`` fields if API consumers need place-level data.

    categories/tags are read-only views of the pin's badges, filtered by kind.
    Badge assignment is handled by the dedicated badges controller/endpoints,
    not through this serializer.
    """

    effective_name = serializers.ReadOnlyField()
    effective_official_name = serializers.ReadOnlyField()
    official_name = serializers.ReadOnlyField()
    effective_icon = serializers.ReadOnlyField()
    latitude = serializers.DecimalField(
        source="location.latitude",
        max_digits=9,
        decimal_places=6,
        read_only=True,
    )

    longitude = serializers.DecimalField(
        source="location.longitude",
        max_digits=9,
        decimal_places=6,
        read_only=True,
    )
    categories = BadgeSerializer(many=True, read_only=True)
    tags = BadgeSerializer(many=True, read_only=True)
    statuses = BadgeSerializer(many=True, read_only=True)

    class Meta:
        model = Pin
        fields = [
            "id",
            "name",
            "name_is_user_provided",
            "official_name",
            "effective_name",
            "effective_official_name",
            "icon",
            "effective_icon",
            "categories",
            "last_visited",
            "latitude",
            "longitude",
            "is_private",
            "created",
            "updated",
            "profile",
            "tags",
            "rating",
            "statuses",
        ]

    def create(self, validated_data):
        if "name_is_user_provided" not in validated_data:
            validated_data["name_is_user_provided"] = bool((validated_data.get("name") or "").strip())
        pin = Pin.objects.create(**validated_data)
        try:
            from urbanlens.dashboard.services.auto_tag import AutoTagService

            AutoTagService().suggest_for_pin(pin, apply=True)
        except (RuntimeError, OSError, ValueError):
            logger.warning("Auto-tagging failed for pin %s", pin.pk, exc_info=True)
        return pin
