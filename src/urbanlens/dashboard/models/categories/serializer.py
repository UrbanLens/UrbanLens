"""CategorySerializer - backed by Label with kind='category'."""

from __future__ import annotations

from rest_framework import serializers

from urbanlens.dashboard.models.labels.model import Label


class CategorySerializer(serializers.ModelSerializer):
    """Serializer for Label rows that represent categories."""

    pin_count = serializers.SerializerMethodField()
    location_count = serializers.SerializerMethodField()

    class Meta:
        model = Label
        fields = ["id", "name", "description", "color", "icon", "order", "pin_count", "location_count"]

    def get_pin_count(self, obj: Label) -> int:
        """Return the number of pins with this category.

        Args:
            obj: The Label instance.

        Returns:
            Pin count.
        """
        return obj.pins.count()

    def get_wiki_count(self, obj: Label) -> int:
        """Return the number of community wikis with this category.

        Args:
            obj: The Label instance.

        Returns:
            Wiki count (exposed as location_count for API compatibility).
        """
        return obj.wikis.count()
