"""Campus serializer."""

from rest_framework import serializers

from urbanlens.dashboard.models.campus.model import Campus


class CampusSerializer(serializers.ModelSerializer):
    """Serializer for Campus - the spatial region boundary for a Location or Pin.

    polygon and generated_polygon are serialized as WKT.  When both are null,
    the client should render a circle with radius default_radius_meters around
    the Location's coordinates instead.
    """

    polygon_wkt = serializers.SerializerMethodField()
    generated_polygon_wkt = serializers.SerializerMethodField()

    def get_polygon_wkt(self, obj) -> str | None:
        return obj.polygon.wkt if obj.polygon else None

    def get_generated_polygon_wkt(self, obj) -> str | None:
        return obj.generated_polygon.wkt if obj.generated_polygon else None

    class Meta:
        model = Campus
        fields = [
            "id",
            "location",
            "profile",
            "pin",
            "polygon_wkt",
            "generated_polygon_wkt",
            "default_radius_meters",
            "created",
            "updated",
        ]
        read_only_fields = ["created", "updated"]
