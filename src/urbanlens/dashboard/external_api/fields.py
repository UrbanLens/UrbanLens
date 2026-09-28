"""Serializer fields shared across the external API's write serializers."""

from __future__ import annotations

from typing import Any

from rest_framework import serializers

from urbanlens.dashboard.models.links.model import MAX_LINK_URL_LENGTH
from urbanlens.dashboard.services.core.icons import MAX_ICON_LENGTH, clean_icon
from urbanlens.dashboard.services.security.link_urls import InvalidLinkUrlError, clean_link_url


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


class LinkUrlField(serializers.CharField):
    """An http(s) link, checked by the rule every link writer shares (``services.security.link_urls``).

    DRF's ``URLField`` also admits ftp, which the link models refuse on save.
    """

    default_error_messages = {"not_a_link": "That doesn't look like a valid http(s) url."}

    def __init__(self, **kwargs: Any) -> None:
        kwargs.setdefault("max_length", MAX_LINK_URL_LENGTH)
        super().__init__(**kwargs)

    def to_internal_value(self, data: Any) -> str:
        value = super().to_internal_value(data)
        try:
            return clean_link_url(value, max_length=self.max_length or MAX_LINK_URL_LENGTH)
        except InvalidLinkUrlError:
            self.fail("not_a_link")
            raise
