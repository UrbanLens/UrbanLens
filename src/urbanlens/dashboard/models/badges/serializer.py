"""Serializer for Badge."""

from __future__ import annotations

from rest_framework import serializers

from urbanlens.dashboard.models.badges.model import Badge


class BadgeSerializer(serializers.ModelSerializer):
    """Serializes Badge for API and HTMX responses."""

    custom_icon_url = serializers.SerializerMethodField()
    pin_count = serializers.SerializerMethodField()

    class Meta:
        model = Badge
        fields = ["id", "name", "description", "color", "icon", "custom_icon_url", "order", "profile", "pin_count"]

    def get_custom_icon_url(self, obj: Badge) -> str | None:
        """Return the absolute URL for the custom icon, if set.

        Args:
            obj: The Badge instance.

        Returns:
            Absolute URL string or None.
        """
        if obj.custom_icon:
            request = self.context.get("request")
            if request:
                return request.build_absolute_uri(obj.custom_icon.url)
            return obj.custom_icon.url
        return None

    def get_pin_count(self, obj: Badge) -> int:
        """Return the number of pins with this badge.

        Args:
            obj: The Badge instance.

        Returns:
            Pin count.
        """
        if (count := getattr(obj, "pin_count", None)) is not None:
            return count
        return obj.pins.count() + obj.categorized_pins.count() + obj.status_pins.count()
