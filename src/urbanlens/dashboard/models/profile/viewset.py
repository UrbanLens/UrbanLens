from rest_framework import viewsets

from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.models.profile.serializer import ProfileSerializer


class ProfileViewSet(viewsets.ModelViewSet):
    queryset = Profile.objects.all()
    serializer_class = ProfileSerializer
