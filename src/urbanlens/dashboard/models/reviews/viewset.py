from __future__ import annotations

import logging

from rest_framework import status, viewsets
from rest_framework.response import Response

from urbanlens.dashboard.models.reviews.model import Review
from urbanlens.dashboard.models.reviews.serializer import ReviewSerializer

logger = logging.getLogger(__name__)


class ReviewViewSet(viewsets.GenericViewSet):
    """Upsert-only REST access to the requesting user's pin ratings.

    Reviews back the personal 0-5 star rating widgets (map popup and pin
    detail page). The only route is the explicit ``create_or_update`` path in
    ``urls.py`` - the app never lists, retrieves, or deletes reviews over
    REST, so no other operations are exposed. A user has at most one review
    per pin; the write path upserts on (profile, pin).
    """

    serializer_class = ReviewSerializer

    def get_queryset(self):
        """Return the requesting user's reviews.

        Returns:
            QuerySet of reviews owned by ``request.user``, or an empty
            queryset when there is no request (e.g. schema generation).
        """
        if not self.request or not self.request.user.is_authenticated:
            return Review.objects.none()
        return Review.objects.filter(profile__user=self.request.user)

    def create_or_update(self, request, pk=None):
        """Upsert the requesting user's review for pin ``pk``.

        ``pk`` is the *pin* id, not a review id - the route exists so the
        frontend can rate a pin without knowing whether a review row already
        exists.

        Returns:
            201 with the serialized review when newly created, 200 when
            updated, or 400 when ``rating`` is missing or the pin does not
            exist or belongs to another user (identical responses, so pin ids
            cannot be enumerated).
        """
        from urbanlens.dashboard.models.pin.model import Pin

        pin = Pin.objects.filter(pk=pk, profile__user=request.user).first()
        if pin is None:
            return Response({"detail": "Unknown pin."}, status=status.HTTP_400_BAD_REQUEST)

        serializer = self.get_serializer(data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        fields = {key: value for key, value in serializer.validated_data.items() if key != "pin"}
        if "rating" not in fields:
            return Response({"detail": "'rating' is required."}, status=status.HTTP_400_BAD_REQUEST)
        review, created = Review.objects.update_or_create(
            profile=request.user.profile,
            pin=pin,
            defaults=fields,
        )
        logger.info("Review %s %s by user %s", review.id, "created" if created else "updated", request.user.id)
        return Response(
            self.get_serializer(review).data,
            status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        )
