"""*********************************************************************************************************************
*                                                                                                                      *
*                                                                                                                      *
*                                                                                                                      *
*                                                                                                                      *
* -------------------------------------------------------------------------------------------------------------------- *
*                                                                                                                      *
*    METADATA:                                                                                                         *
*                                                                                                                      *
*        File:    viewset.py                                                                                           *
*        Path:    /dashboard/models/reviews/viewset.py                                                                 *
*        Project: urbanlens                                                                                            *
*        Version: 1.0.0                                                                                                *
*        Created: 2023-12-24                                                                                           *
*        Author:  Jess Mann                                                                                            *
*        Email:   jess@manlyphotos.com                                                                                 *
*        Copyright (c) 2024 Urban Lens                                                                                 *
*                                                                                                                      *
* -------------------------------------------------------------------------------------------------------------------- *
*                                                                                                                      *
*    LAST MODIFIED:                                                                                                    *
*                                                                                                                      *
*        2023-12-24     By Jess Mann                                                                                   *
*                                                                                                                      *
*********************************************************************************************************************"""
import logging
from rest_framework import viewsets, status
from rest_framework.response import Response
from dashboard.models.reviews.model import Review
from dashboard.models.reviews.serializer import ReviewSerializer

logger = logging.getLogger(__name__)

class ReviewViewSet(viewsets.ModelViewSet):
    serializer_class = ReviewSerializer
    basename = 'reviews'

    def get_queryset(self):
        if not self.request:
            return Review.objects.none()
        return Review.objects.filter(user=self.request.user)
    
    def create(self, request, *args, **kwargs):
        logger.info(f"Create request initiated by user {request.user.id}")
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        self.perform_create(serializer)
        logger.info(f"Review created with id {serializer.data['id']}")
        return Response(serializer.data, status=status.HTTP_201_CREATED)
    
    def perform_create(self, serializer):
        serializer.save(user=self.request.user)

    def update(self, request, *args, **kwargs):
        logger.info(f"Update request initiated by user {request.user.id}")
        instance = self.get_object()
        if instance.user != request.user:
            logger.error("User %s attempted to update review %s, but does not have permission", request.user.id, instance.id)
            return Response(status=status.HTTP_403_FORBIDDEN)
        serializer = self.get_serializer(instance, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        self.perform_update(serializer)
        logger.info(f"Review with id {instance.id} updated")
        return Response(serializer.data)
    
    def destroy(self, request, *args, **kwargs):
        logger.info(f"Delete request initiated by user {request.user.id}")
        instance = self.get_object()
        if instance.user != request.user:
            logger.error("User %s attempted to delete review %s, but does not have permission", request.user.id, instance.id)
            return Response(status=status.HTTP_403_FORBIDDEN)
        logger.info(f"Review with id {instance.id} deleted")
        return super().destroy(request, *args, **kwargs)
