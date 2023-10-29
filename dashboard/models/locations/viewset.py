from rest_framework import viewsets, status, pagination
from rest_framework.response import Response
from .model import Location
from .serializer import LocationSerializer
from .filterset import LocationFilter

class LocationViewSet(viewsets.ModelViewSet):
    filter_class = LocationFilter
    def get_queryset(self):
        queryset = Location.objects.filter(user=self.request.user)
        filter_backends = [filters.DjangoFilterBackend]
        filterset_class = LocationFilter
        return queryset
    serializer_class = LocationSerializer

    import logging
    logger = logging.getLogger(__name__)

    def create(self, request, *args, **kwargs):
        logger.info(f"Create request initiated by user {request.user.id}")
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
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
