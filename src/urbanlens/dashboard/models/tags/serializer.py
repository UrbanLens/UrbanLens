"""Serializer for Tag."""

from __future__ import annotations

from rest_framework import serializers

from urbanlens.dashboard.models.tags.model import Tag


class TagSerializer(serializers.ModelSerializer):
    """Serializes Tag for API and HTMX responses."""

    custom_icon_url = serializers.SerializerMethodField()
    pin_count = serializers.SerializerMethodField()

    class Meta:
        model = Tag
        fields = ["id", "name", "description", "color", "icon", "custom_icon_url", "order", "profile", "pin_count"]

    def get_custom_icon_url(self, obj: Tag) -> str | None:
        if obj.custom_icon:
            request = self.context.get("request")
            if request:
                return request.build_absolute_uri(obj.custom_icon.url)
            return obj.custom_icon.url
        return None

    def get_pin_count(self, obj: Tag) -> int:
        return obj.pins.count()
