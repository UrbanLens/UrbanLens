"""CategorySerializer."""

from rest_framework import serializers

from urbanlens.dashboard.models.categories.model import Category


class CategorySerializer(serializers.ModelSerializer):
    """Serializer for Category with pin and location counts."""

    pin_count = serializers.SerializerMethodField()
    location_count = serializers.SerializerMethodField()

    class Meta:
        model = Category
        fields = ["id", "name", "description", "color", "icon", "order", "pin_count", "location_count"]

    def get_pin_count(self, obj: Category) -> int:
        """Return the number of pins with this category.

        Args:
            obj: The Category instance.

        Returns:
            Pin count.
        """
        return obj.pins.count()

    def get_location_count(self, obj: Category) -> int:
        """Return the number of locations with this category.

        Args:
            obj: The Category instance.

        Returns:
            Location count.
        """
        return obj.locations.count()
