from rest_framework import viewsets
from .model import Friendship
from .serializer import FriendshipSerializer

class FriendshipViewSet(viewsets.ModelViewSet):
    queryset = Friendship.objects.all()
    serializer_class = FriendshipSerializer
