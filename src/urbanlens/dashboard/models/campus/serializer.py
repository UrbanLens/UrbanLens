"""Campus serializer."""

from rest_framework import serializers

from urbanlens.dashboard.models.campus.model import Campus


class CampusSerializer(serializers.ModelSerializer):
    """Serializer for Campus — the spatial region boundary for a Location.

    polygon is serialized as WKT.  When polygon is null, the client should
    render a circle with radius default_radius_meters around the Location's
    coordinates instead.
    """

    # Expose polygon as WKT string; null when no polygon is stored.
    polygon_wkt = serializers.SerializerMethodField()

    def get_polygon_wkt(self, obj) -> str | None:
        return obj.polygon.wkt if obj.polygon else None

    class Meta:
        model = Campus
        fields = [
            "id",
            "location",
            "profile",
            "polygon_wkt",
            "default_radius_meters",
            "created",
            "updated",
        ]
        read_only_fields = ["created", "updated"]
