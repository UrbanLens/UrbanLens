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
