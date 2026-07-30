from __future__ import annotations

import logging

from rest_framework import mixins, status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.reviews.model import Review
from urbanlens.dashboard.models.reviews.serializer import ReviewSerializer
from urbanlens.dashboard.services.reviews import upsert_review

logger = logging.getLogger(__name__)


class ReviewViewSet(mixins.UpdateModelMixin, mixins.DestroyModelMixin, viewsets.GenericViewSet):
    """Review endpoints.

    Only ``create_or_update`` (bound directly via ``as_view`` in
    ``dashboard/urls.py``) is actually routed - this viewset is never
    registered with the DRF router, so ``list``/``create``/``retrieve``
    are never reachable. The base class only mixes in ``update``/``destroy``
    (used by :meth:`update`/:meth:`destroy` below, kept in case they're
    wired up later) rather than the full ``ModelViewSet``, which previously
    exposed an unrouted, broken ``create()`` that violated the
    ``unique_together`` constraint by saving twice.
    """

    serializer_class = ReviewSerializer
    basename = "reviews"

    def get_queryset(self):
        if not self.request:
            return Review.objects.none()
        return Review.objects.all().filter(profile__user=self.request.user)

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

        # Validate first, then delegate the upsert to services.reviews so this
        # and the external API share one implementation of the
        # one-rating-per-(profile, pin) rule.
        review = Review.objects.for_pair(profile, pin).first()
        serializer = self.get_serializer(review, data=request.data, partial=True) if review is not None else self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        # `rating` is optional on a partial update, in which case the existing
        # value stands - preserving the pre-refactor no-op behavior rather than
        # blowing up on a missing key.
        rating = serializer.validated_data.get("rating", review.rating if review is not None else None)
        review, created = upsert_review(profile, pin, rating)

        return Response(self.get_serializer(review).data, status=status.HTTP_201_CREATED if created else status.HTTP_200_OK)

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
