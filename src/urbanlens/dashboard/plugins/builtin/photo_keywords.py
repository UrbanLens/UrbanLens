"""
Built-in photo keywording plugins.
Three independent strategies, each storing its own ``ImageKeyword`` rows so they coexist and can be regenerated separately:"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, ClassVar

from urbanlens.dashboard.plugins.base import UrbanLensPlugin
from urbanlens.dashboard.services.photos.photo_keywords import KeywordResult, PhotoKeywordProvider

if TYPE_CHECKING:
    from urbanlens.dashboard.models.images.model import Image
    from urbanlens.dashboard.plugins.base import PluginContext

logger = logging.getLogger(__name__)


class MetadataKeywordProvider(PhotoKeywordProvider):
    """Keywords the photographer embedded in the file (XMP/IPTC), as the upload task read them in the sandbox."""

    slug = "photo_keywords_metadata"
    label = "Embedded metadata keywords"

    def is_available_for(self, image: Image) -> bool:
        """
        Only when the upload task recorded keywords.

        An empty record cannot tell a file with none from one rewritten before it was read, so it never
        replaces the keywords a photo already has.

        Args:
            image: The uploaded image.

        Returns:
            True when ``Image.embedded_keywords`` holds at least one keyword.
        """
        return bool(image.embedded_keywords)

    def generate(self, image: Image) -> list[KeywordResult]:
        """
        Return the keywords recorded on the row; the stored file is never opened here.

        Args:
            image: The uploaded image.

        Returns:
            Embedded keywords; empty when the file carried none.
        """
        recorded = image.embedded_keywords
        if not isinstance(recorded, list):
            return []
        return [KeywordResult(keyword=keyword) for keyword in recorded if isinstance(keyword, str)]


class PhotoMetadataKeywordsPlugin(UrbanLensPlugin):
    """Keywords from XMP/IPTC tags embedded in uploaded photos."""

    name: ClassVar[str] = "photo_keywords_metadata"
    verbose_name: ClassVar[str] = "Photo keywords: embedded metadata"
    description: ClassVar[str] = "Makes photos searchable using the XMP/IPTC keyword tags photographers embed in their files. Runs locally; no external calls."
    author: ClassVar[str] = "UrbanLens"

    def setup(self, plugin: PluginContext) -> None:
        """The embedded-metadata photo-keyword provider."""
        from urbanlens.dashboard.plugins.points import PHOTO_KEYWORD_PROVIDERS

        plugin.extend(PHOTO_KEYWORD_PROVIDERS, [MetadataKeywordProvider()])
