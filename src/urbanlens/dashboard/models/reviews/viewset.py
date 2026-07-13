from __future__ import annotations

import logging

from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.reviews.model import Review
from urbanlens.dashboard.models.reviews.serializer import ReviewSerializer

logger = logging.getLogger(__name__)


class ReviewViewSet(viewsets.ModelViewSet):
    serializer_class = ReviewSerializer
    basename = "reviews"

    def get_queryset(self):
        if not self.request:
            return Review.objects.none()
        return Review.objects.all().filter(profile__user=self.request.user)

    def create(self, request, pin_id, *args, **kwargs):
        logger.info("Create request initiated by user %s", request.user.id)
        data = request.data
        data["profile"] = request.user.profile
        data["pin"] = pin_id
        # Check if the review already exists for the given pin and profile
        review, created = Review.objects.get_or_create(
            profile=request.user.profile,
            pin_id=pin_id,
            defaults=data,
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
        logger.info("Review created with id %s", serializer.data["id"])
        return Response(serializer.data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=["patch"], url_path="create_or_update", url_name="create_or_update")
    def create_or_update(self, request, pk=None):
        """Upsert the requester's own rating for a pin (star-rating widget).

        ``pk`` is the target pin's id, not a Review id - this always acts on
        the caller's own (profile, pin) rating, creating it on first use and
        updating it thereafter. ``profile`` and ``pin`` are never taken from
        the request body; only ``rating`` is client-controlled.
        """
        profile = request.user.profile
        pin = Pin.objects.filter(pk=pk, profile=profile).first()
        if pin is None:
            # Foreign and nonexistent pins get an identical response, so pin
            # ids cannot be enumerated by probing this endpoint.
            return Response({"detail": "Pin not found."}, status=status.HTTP_400_BAD_REQUEST)

        review = Review.objects.filter(profile=profile, pin=pin).first()
        if review is None:
            serializer = self.get_serializer(data=request.data)
            serializer.is_valid(raise_exception=True)
            serializer.save(profile=profile, pin=pin)
            created = True
        else:
            serializer = self.get_serializer(review, data=request.data, partial=True)
            serializer.is_valid(raise_exception=True)
            serializer.save()
            created = False

        return Response(serializer.data, status=status.HTTP_201_CREATED if created else status.HTTP_200_OK)

    def perform_create(self, serializer):
        serializer.save(profile=self.request.user.profile)

    def update(self, request, *args, **kwargs):
        logger.info("Update request initiated by user %s", request.user.id)
        instance = self.get_object()
        if instance.profile.user != request.user:
            logger.error(
                "User %s attempted to update review %s, but does not have permission",
                request.user.id,
                instance.id,
            )
            return Response(status=status.HTTP_403_FORBIDDEN)
        data = request.data
        data["profile"] = request.user.profile.id
        serializer = self.get_serializer(instance, data=data, partial=True)
        serializer.is_valid(raise_exception=True)
        self.perform_update(serializer)
        logger.info("Review with id %s updated", instance.id)
        return Response(serializer.data)

    def destroy(self, request, *args, **kwargs):
        logger.info("Delete request initiated by user %s", request.user.id)
        instance = self.get_object()
        if instance.profile.user != request.user:
            logger.error(
                "User %s attempted to delete review %s, but does not have permission",
                request.user.id,
                instance.id,
            )
            return Response(status=status.HTTP_403_FORBIDDEN)
        logger.info("Review with id %s deleted", instance.id)
        return super().destroy(request, *args, **kwargs)
