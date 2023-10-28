from rest_framework import serializers
from .model import Location

class LocationSerializer(serializers.ModelSerializer):
    class Meta:
        model = Location
        fields = ['name', 'latitude', 'longitude', 'created', 'updated', 'profile', 'user']

    def create(self, validated_data):
        user = validated_data.pop('user')
        location = Location.objects.create(**validated_data)
        location.user = user
        location.save()
        return location
