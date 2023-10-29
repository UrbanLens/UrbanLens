from rest_framework import serializers
from .model import Friendship

class FriendshipSerializer(serializers.ModelSerializer):
    class Meta:
        model = Friendship
        fields = ['user', 'friend']
