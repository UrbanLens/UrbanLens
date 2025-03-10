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
*        Version: 0.0.2                                                                                                *
*        Created: 2023-12-24                                                                                           *
*        Author:  Jess Mann                                                                                            *
*        Email:   jess@urbanlens.org                                                                                 *
*        Copyright (c) 2025 Jess Mann                                                                                  *
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
from rest_framework.decorators import action
from urbanlens.dashboard.models.reviews.model import Review
from urbanlens.dashboard.models.reviews.serializer import ReviewSerializer

logger = logging.getLogger(__name__)

class ReviewViewSet(viewsets.ModelViewSet):
    serializer_class = ReviewSerializer
    basename = 'reviews'
    def get_queryset(self):
        if not self.request:
            return Review.objects.none()
        return Review.objects.filter(user=self.request.user)

    def create(self, request, pin_id, *args, **kwargs):
        logger.info(f"Create request initiated by user {request.user.id}")
        data = request.data
        data['user'] = request.user
        data['pin'] = pin_id
        # Check if the review already exists for the given pin and user
        review, created = Review.objects.get_or_create(
            user=request.user,
            pin_id=pin_id,
            defaults=data
        )
        if not created:
            for key, value in data.items():
                setattr(review, key, value)
            review.save()
            serializer = self.get_serializer(review)
        else:
            serializer = self.get_serializer(data=data)
            serializer.is_valid(raise_exception=True)
            self.perform_create(serializer)
        logger.info(f"Review created with id {serializer.data['id']}")
        return Response(serializer.data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=['patch'], url_path='create_or_update', url_name='create_or_update')
    def create_or_update(self, request, pk=None):
        pin_id = pk
        data = request.data.copy()
        data['pin_id'] = pin_id

        review, created = Review.objects.get_or_create(
            user=request.user,
            pin_id=pin_id,
            defaults=data
        )

        if created:
            serializer = self.get_serializer(review)
        else:
            serializer = self.get_serializer(review, data=data, partial=True)
            serializer.is_valid(raise_exception=True)
            review = serializer.save()

        return Response(serializer.data, status=status.HTTP_200_OK if not created else status.HTTP_201_CREATED)

    def perform_create(self, serializer):
        serializer.save(user=self.request.user)

    def update(self, request, *args, **kwargs):
        logger.info(f"Update request initiated by user {request.user.id}")
        instance = self.get_object()
        if instance.user != request.user:
            logger.error("User %s attempted to update review %s, but does not have permission", request.user.id, instance.id)
            return Response(status=status.HTTP_403_FORBIDDEN)
        data = request.data
        data['user'] = request.user.id
        serializer = self.get_serializer(instance, data=data, partial=True)
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
