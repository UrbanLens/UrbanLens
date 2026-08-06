import logging

from rest_framework import serializers

from urbanlens.dashboard.models.labels.serializer import LabelSerializer
from urbanlens.dashboard.models.pin.model import Pin

logger = logging.getLogger(__name__)


class PinSerializer(serializers.ModelSerializer):
    """Serializer for Pin - exposes user-specific fields only.

    Canonical coordinates are read from the related Location (``pin.location``);
    ``latitude``/``longitude`` are read-only here. A coordinate move (map pin
    dragging) is handled separately by ``PinViewSet.partial_update``, which
    repoints ``pin.location`` directly rather than writing through this
    serializer - Location has no per-pin writable representation here.

    Address and place name are not included here; nest a LocationSerializer or add
    read-only ``source="location.*"`` fields if API consumers need place-level data.

    categories/tags are read-only views of the pin's labels, filtered by kind.
    Label assignment is handled by the dedicated labels controller/endpoints,
    not through this serializer.
    """

    effective_name = serializers.ReadOnlyField()
    name_is_user_provided = serializers.ReadOnlyField()
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
    # Owner is always set server-side (see PinViewSet.perform_update) - never
    # accepted from the client, or a PATCH could reassign a pin to an
    # arbitrary profile id.
    profile = serializers.PrimaryKeyRelatedField(read_only=True)
    categories = LabelSerializer(many=True, read_only=True)
    tags = LabelSerializer(many=True, read_only=True)
    statuses = LabelSerializer(many=True, read_only=True)

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
            "created",
            "updated",
            "profile",
            "tags",
            "rating",
            "statuses",
        ]

    def create(self, validated_data):
        # A create (including an import through this serializer) is not an
        # explicit rename.  Only the pin-name editing flows set this guard.
        validated_data["name_is_user_provided"] = False
        pin = Pin.objects.create(**validated_data)
        # Enqueued, not run here: AutoTagService falls through to the LLM gateway for
        # anything its keyword stage misses, so doing this inline made every pin created
        # through the REST API or an import wait on a network round-trip before the
        # response came back. The other two creation paths
        # (services.pins.pin_creation and the Google Maps import) already enqueue this
        # same task; this call site was the one left behind. Tagging was always
        # best-effort - the previous code swallowed its failures - so nothing in the
        # response depends on it having run.
        from urbanlens.dashboard.services.core.celery import safely_enqueue_task
        from urbanlens.dashboard.tasks import suggest_pin_category

        safely_enqueue_task(suggest_pin_category, pin.pk)
        return pin

    def update(self, instance, validated_data):
        """Mark the guard only when this edit actually changes the name."""
        if "name" in validated_data and (validated_data.get("name") or "").strip() != (instance.name or "").strip():
            validated_data["name_is_user_provided"] = bool((validated_data.get("name") or "").strip())
        return super().update(instance, validated_data)
