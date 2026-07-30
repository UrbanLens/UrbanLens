"""Request/response types for a safety check-in's live location.

Deliberately its own module, not folded into ``serializers.py``'s
``SafetyCheckinDetailSerializer`` - see that serializer's docstring on why
live location was excluded from the general check-in document, and
``views_safety_chat``/``mixins_safety`` for the shared owner-or-accepted-
partner visibility this reuses.
"""

from __future__ import annotations

from rest_framework import serializers


class SafetyCheckinLocationSerializer(serializers.Serializer):
    """The owner's current shared position on a check-in, as read by the owner or a watching partner.

    ``latitude``/``longitude``/``accuracy`` are null whenever ``sharing_enabled``
    is false - disabling sharing clears the last-known fix rather than leaving
    a stale marker visible to partners (see
    ``services.safety.set_live_location_sharing``), so there is nothing extra
    to scrub here.
    """

    sharing_enabled = serializers.BooleanField(read_only=True, source="live_location_sharing_enabled")
    latitude = serializers.FloatField(read_only=True, allow_null=True, source="live_latitude")
    longitude = serializers.FloatField(read_only=True, allow_null=True, source="live_longitude")
    accuracy = serializers.FloatField(read_only=True, allow_null=True, source="live_location_accuracy")
    updated_at = serializers.DateTimeField(read_only=True, allow_null=True, source="live_location_updated_at")


class SafetyCheckinLocationUpdateSerializer(serializers.Serializer):
    """Validates a live-location PATCH. Owner-only; every field is optional and independent.

    ``sharing_enabled`` is applied before a position, so a single PATCH may
    both turn sharing on and report the first fix in one round trip.
    ``latitude``/``longitude`` must be submitted together - a fix is a pair or
    it is not a fix.
    """

    sharing_enabled = serializers.BooleanField(required=False)
    latitude = serializers.FloatField(required=False, min_value=-90, max_value=90)
    longitude = serializers.FloatField(required=False, min_value=-180, max_value=180)
    accuracy = serializers.FloatField(required=False, allow_null=True, min_value=0)

    def validate(self, attrs: dict) -> dict:
        """Require latitude and longitude together, never one alone.

        Args:
            attrs: The already field-validated submission.

        Returns:
            The unchanged, validated attributes.

        Raises:
            serializers.ValidationError: If exactly one of latitude/longitude
                was submitted.
        """
        if ("latitude" in attrs) != ("longitude" in attrs):
            raise serializers.ValidationError("latitude and longitude must be submitted together.")
        return attrs
