"""Serializer for the Image model."""

from rest_framework import serializers

from urbanlens.dashboard.models.images.model import Image


class ImageSerializer(serializers.ModelSerializer):
    image_url = serializers.SerializerMethodField()
    uploader_username = serializers.SerializerMethodField()

    class Meta:
        model = Image
        fields = ["id", "image", "image_url", "pin", "wiki", "profile", "uploader_username", "caption", "latitude", "longitude", "created"]
        read_only_fields = ["id", "image_url", "uploader_username", "created"]

    def get_image_url(self, obj: Image) -> str | None:
        request = self.context.get("request")
        if obj.image and hasattr(obj.image, "url"):
            url = obj.image.url
            return request.build_absolute_uri(url) if request else url
        return None

    def get_uploader_username(self, obj: Image) -> str | None:
        if obj.profile:
            return obj.profile.username
        return None
