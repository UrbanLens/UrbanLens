"""Schema types for the pin-comment reaction endpoint.

Response-shape declarations only - the reaction endpoints take no request body (the emoji travels in
the URL path, and PUT/DELETE carry the intent), so there is nothing here to validate, only something
to document.
"""

from __future__ import annotations

from rest_framework import serializers


class ReactionCountSerializer(serializers.Serializer):
    """One emoji's tally on a comment, from the requesting viewer's perspective."""

    #: How many profiles have reacted with this emoji, the caller included.
    count = serializers.IntegerField(read_only=True)
    #: Whether the caller is one of them - the flag a client renders its
    #: highlighted/selected reaction chip from.
    reacted = serializers.BooleanField(read_only=True)


class CommentReactionsSerializer(serializers.Serializer):
    """The body both reaction handlers return: the target's full, fresh summary.

    Deliberately the *whole* summary rather than a delta for the emoji that was just changed.
    A declarative PUT/DELETE that answers with complete state lets a client replace its local copy
    outright, so a dropped response or an out-of-order retry cannot leave the UI showing a count that
    drifted from the server's.
    """

    #: ``{emoji: {"count": int, "reacted": bool}}``. A free-form mapping rather than declared keys, because the
    #: emoji vocabulary (``services.comments.comments.ALLOWED_EMOJIS``) is a product decision that changes
    #: without an API version bump.
    reactions = serializers.DictField(child=ReactionCountSerializer(), read_only=True)
