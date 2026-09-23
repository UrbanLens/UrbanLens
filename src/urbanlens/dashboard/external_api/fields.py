"""Serializer fields shared across the external API's write serializers."""

from __future__ import annotations

from typing import Any

from rest_framework import serializers

from urbanlens.dashboard.services.core.icons import MAX_ICON_LENGTH, clean_icon


class IconField(serializers.CharField):
    """An icon value: a picker catalogue entry, a Material icon name, an uploaded icon's URL, or one emoji.

    The column is rendered into marker, chip and popup HTML, so anything else is refused here rather than
    stored and escaped later.
    """

    default_error_messages = {"not_an_icon": "Use a Material icon name, a single emoji, or an icon URL."}

    def to_internal_value(self, data: Any) -> str:
        value = super().to_internal_value(data)
        if value and clean_icon(value, max_length=self.max_length or MAX_ICON_LENGTH) is None:
            self.fail("not_an_icon")
        return value
