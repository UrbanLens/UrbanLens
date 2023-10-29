from rest_framework import viewsets, status
from rest_framework.response import Response
from .model import Location
from .serializer import LocationSerializer
from .filterset import LocationFilter

class LocationViewSet(viewsets.ModelViewSet):
    filter_class = LocationFilter
    def get_queryset(self):
        queryset = Location.objects.filter(user=self.request.user)
        category = self.request.query_params.get('category', None)
        if category is not None:
            queryset = queryset.filter(categories__name=category)
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
