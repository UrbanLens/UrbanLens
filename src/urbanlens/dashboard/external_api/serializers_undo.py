"""Serializers for the external API's undo domain."""

from __future__ import annotations

from rest_framework import serializers


class UndoEntrySerializer(serializers.Serializer):
    """One recently deleted item the caller may still restore."""

    uuid = serializers.UUIDField(read_only=True)
    model_label = serializers.CharField(read_only=True)
    object_repr = serializers.CharField(read_only=True)
    created = serializers.DateTimeField(read_only=True)
    expires_at = serializers.DateTimeField(read_only=True)


class UndoHistorySerializer(serializers.Serializer):
    """The undo feed, as the endpoint actually returns it.

    Exists because the endpoint was documented as a bare array of
    :class:`UndoEntrySerializer` while returning this envelope, so a generated
    client iterated an object. The envelope is the correct half of that
    disagreement: ``omitted`` is load-bearing, and dropping it to match the old
    declaration would remove a client's only signal that its credential is
    missing a scope (see ``views_undo.UndoListView``).
    """

    entries = UndoEntrySerializer(many=True, read_only=True)
    omitted = serializers.ListField(
        child=serializers.CharField(),
        read_only=True,
        help_text="Model labels dropped from `entries` because the credential lacks the paired domain-read scope. Prompt the user to re-authorize rather than rendering an incomplete list.",
    )
