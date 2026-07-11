"""Boundary serializer."""

from rest_framework import serializers

from urbanlens.dashboard.models.boundary.model import Boundary


class BoundarySerializer(serializers.ModelSerializer):
    """Serializer for Boundary - a typed spatial region for a Location, Wiki, or Pin.

    polygon and generated_polygon are serialized as WKT.  When both are null on
    a property boundary, the client should render a circle with radius
    default_radius_meters around the Location's coordinates instead.
    """

    polygon_wkt = serializers.SerializerMethodField()
    generated_polygon_wkt = serializers.SerializerMethodField()

    def get_polygon_wkt(self, obj) -> str | None:
        """WKT of the user/community-drawn polygon, or None.

        Args:
            obj: The Boundary being serialized.

        Returns:
            WKT string or None.
        """
        return obj.polygon.wkt if obj.polygon else None

    def get_generated_polygon_wkt(self, obj) -> str | None:
        """WKT of the API-generated polygon, or None.

        Args:
            obj: The Boundary being serialized.

        Returns:
            WKT string or None.
        """
        return obj.generated_polygon.wkt if obj.generated_polygon else None

    class Meta:
        model = Boundary
        fields = [
            "id",
            "boundary_type",
            "location",
            "wiki",
            "profile",
            "pin",
            "polygon_wkt",
            "generated_polygon_wkt",
            "default_radius_meters",
            "generated_at",
            "created",
            "updated",
        ]
        read_only_fields = ["created", "updated", "generated_at"]
