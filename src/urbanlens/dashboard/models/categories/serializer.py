"""CategorySerializer - backed by Tag with kind='category'."""

from __future__ import annotations

from rest_framework import serializers

from urbanlens.dashboard.models.tags.model import Tag


class CategorySerializer(serializers.ModelSerializer):
    """Serializer for Tag rows that represent categories."""

    pin_count = serializers.SerializerMethodField()
    location_count = serializers.SerializerMethodField()

    class Meta:
        model = Tag
        fields = ["id", "name", "description", "color", "icon", "order", "pin_count", "location_count"]

    def get_pin_count(self, obj: Tag) -> int:
        """Return the number of pins with this category.

        Args:
            obj: The Tag instance.

        Returns:
            Pin count.
        """
        return obj.categorized_pins.count()

    def get_location_count(self, obj: Tag) -> int:
        """Return the number of locations with this category.

        Args:
            obj: The Tag instance.

        Returns:
            Location count.
        """
        return obj.categorized_locations.count()
