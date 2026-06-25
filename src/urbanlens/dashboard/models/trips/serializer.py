from rest_framework import serializers

from urbanlens.dashboard.models.trips.model import Trip


class TripSerializer(serializers.ModelSerializer):
    class Meta:
        model = Trip
        fields = ["name", "description", "start_date", "end_date", "created", "updated", "status", "tags"]

    def create(self, validated_data):
        user = self.context["request"].user
        trip = Trip.objects.create(**validated_data)
        trip.profiles.add(user.profile)
        return trip
