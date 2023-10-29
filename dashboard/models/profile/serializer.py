from rest_framework import serializers
from .model import Profile

class ProfileSerializer(serializers.ModelSerializer):
    class Meta:
        model = Profile
        fields = ['user', 'created', 'updated', 'avatar']
