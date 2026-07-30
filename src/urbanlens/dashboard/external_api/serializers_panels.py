"""Serializers for the external API's panel domain.

A panel's detail body is its own ``PanelSource.api_payload()`` dict, passed
through unserialized - its shape is declared per :class:`PanelApiKind`
(``info``/``media``/``boundary``/``buildings``), not fixed, so no single
serializer could describe it without either flattening that variance away or
duplicating each panel's contract here.
"""

from __future__ import annotations

from rest_framework import serializers


class PanelListEntrySerializer(serializers.Serializer):
    """One panel available on a pin, from the ``panels/`` listing endpoint."""

    key = serializers.CharField(read_only=True)
    kinds = serializers.ListField(child=serializers.CharField(), read_only=True)
    ready = serializers.BooleanField(read_only=True)


class PanelPendingSerializer(serializers.Serializer):
    """Body of a 202 response: the panel's fetch was just scheduled or is already in flight."""

    ready = serializers.BooleanField(default=False)
    poll_after_seconds = serializers.IntegerField(read_only=True)
