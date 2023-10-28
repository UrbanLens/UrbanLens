from rest_framework import serializers
from .model import Location

class LocationSerializer(serializers.ModelSerializer):
    class Meta:
        model = Location
        fields = ['name', 'latitude', 'longitude', 'created', 'updated', 'profile']
