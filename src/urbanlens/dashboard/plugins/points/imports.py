"""Imports: how uploaded documents that are not location formats become pins."""

from __future__ import annotations

from typing import TYPE_CHECKING

from urbanlens.dashboard.plugins.extensions import ExtensionPoint

if TYPE_CHECKING:
    from urbanlens.dashboard.services.import_formats.documents import DocumentExtractor


DOCUMENT_EXTRACTORS: ExtensionPoint[DocumentExtractor] = ExtensionPoint(
    "imports.document_extractors",
    "Readers of uploaded documents that turn their text into a pin list, asked in priority order for the first that handles a file's extension. A document no extractor handles is not accepted.",
)

__all__ = ["DOCUMENT_EXTRACTORS"]
