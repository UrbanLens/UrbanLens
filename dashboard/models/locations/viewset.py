from rest_framework import viewsets, status
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

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        self.perform_create(serializer)
        headers = self.get_success_headers(serializer.data)
        return Response(serializer.data, status=status.HTTP_201_CREATED, headers=headers)

    def perform_create(self, serializer):
        serializer.save(user=self.request.user, profile=self.request.user.profile)

    def update(self, request, *args, **kwargs):
        instance = self.get_object()
        if instance.user != request.user:
            return Response(status=status.HTTP_403_FORBIDDEN)
        return super().update(request, *args, **kwargs)

    def destroy(self, request, *args, **kwargs):
        instance = self.get_object()
        if instance.user != request.user:
            return Response(status=status.HTTP_403_FORBIDDEN)
        return super().destroy(request, *args, **kwargs)
