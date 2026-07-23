from rest_framework import serializers

from urbanlens.dashboard.models.labels.serializer import LabelSerializer
from urbanlens.dashboard.models.wiki.model import Wiki


class WikiSerializer(serializers.ModelSerializer):
    official_name = serializers.ReadOnlyField()
    latitude = serializers.ReadOnlyField()
    longitude = serializers.ReadOnlyField()
    categories = LabelSerializer(many=True, read_only=True)
    tags = LabelSerializer(many=True, read_only=True)
    statuses = LabelSerializer(many=True, read_only=True)

    class Meta:
        model = Wiki
        fields = ["slug", "name", "official_name", "description", "categories", "latitude", "longitude", "created", "updated", "tags", "statuses"]

    def create(self, validated_data):
        wiki = Wiki.objects.create(**validated_data)
        wiki.save()
        return wiki
