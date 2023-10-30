import logging
from rest_framework import viewsets, status
from rest_framework.response import Response
from django_filters.rest_framework import DjangoFilterBackend
from .model import Location
from .serializer import LocationSerializer

logger = logging.getLogger(__name__)

class LocationViewSet(viewsets.ModelViewSet):
    def get_queryset(self):
        return Location.objects.filter(user=self.request.user)
    serializer_class = LocationSerializer

    def create(self, request, *args, **kwargs):
        logger.info(f"Create request initiated by user {request.user.id}")
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        latitude = serializer.validated_data.get('latitude')
        longitude = serializer.validated_data.get('longitude')
        nearby_locations = Location.objects.nearby_locations(latitude, longitude, radius=0.1)  # radius in km
        if nearby_locations.exists():
            return Response({"detail": "A location already exists within a small radius."}, status=status.HTTP_400_BAD_REQUEST)
        self.perform_create(serializer)
        headers = self.get_success_headers(serializer.data)
        logger.info(f"Location created with id {serializer.data['id']}")
        return Response(serializer.data, status=status.HTTP_201_CREATED, headers=headers)

    def perform_create(self, serializer):
        serializer.save(user=self.request.user, profile=self.request.user.profile, status=self.request.data.get('status', Location.WISH_TO_VISIT))

    def update(self, request, *args, **kwargs):
        logger.info(f"Update request initiated by user {request.user.id}")
        instance = self.get_object()
        if instance.user != request.user:
            return Response(status=status.HTTP_403_FORBIDDEN)
        instance.status = request.data.get('status', instance.status)
        instance.save()
        logger.info(f"Location with id {instance.id} updated")
        return super().update(request, *args, **kwargs)

    def destroy(self, request, *args, **kwargs):
        logger.info(f"Delete request initiated by user {request.user.id}")
        instance = self.get_object()
        if instance.user != request.user:
            return Response(status=status.HTTP_403_FORBIDDEN)
        logger.info(f"Location with id {instance.id} deleted")
        return super().destroy(request, *args, **kwargs)
