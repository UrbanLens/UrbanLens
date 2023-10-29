from rest_framework import viewsets, status
from rest_framework.response import Response
from .model import Friendship
from .serializer import FriendshipSerializer

class FriendshipViewSet(viewsets.ModelViewSet):
    queryset = Friendship.objects.all()
    serializer_class = FriendshipSerializer

    def create(self, request, *args, **kwargs):
        friend = User.objects.get(id=request.data.get('friend_id'))
        if Friendship.objects.filter(user=request.user, friend=friend).exists():
            return Response({"detail": "Friendship already exists"}, status=status.HTTP_400_BAD_REQUEST)
        Friendship.objects.create(user=request.user, friend=friend)
        return Response({"detail": "Friendship created"}, status=status.HTTP_201_CREATED)

    def destroy(self, request, *args, **kwargs):
        friend = User.objects.get(id=request.data.get('friend_id'))
        friendship = Friendship.objects.filter(user=request.user, friend=friend)
        if not friendship.exists():
            return Response({"detail": "Friendship does not exist"}, status=status.HTTP_400_BAD_REQUEST)
        friendship.delete()
        return Response({"detail": "Friendship deleted"}, status=status.HTTP_204_NO_CONTENT)
