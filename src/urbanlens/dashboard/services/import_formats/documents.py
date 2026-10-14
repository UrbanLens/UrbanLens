"""Uploaded documents that become pins through an extractor: text or Word files read as prose, not as a location format."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any, ClassVar

from urbanlens.dashboard.plugins.points import DOCUMENT_EXTRACTORS

if TYPE_CHECKING:
    from urbanlens.dashboard.models.profile.model import Profile


class DocumentTooLargeError(Exception):
    """A document, or the text extracted from it, is over its extractor's limit. It is refused, not truncated."""


class DocumentExtractor(ABC):
    """
    Turns an uploaded document into a pin list.

    The text half (:meth:`read_text`) runs where uploads are parsed and must not reach the network; the pin half
    (:meth:`extract_pins`) runs on an interactive worker, and only where :meth:`available` says it may.
    """

    #: Lowercase extensions without the dot.
    extensions: ClassVar[frozenset[str]]
    #: The largest upload this extractor reads; a larger file is reported too large without being read.
    max_bytes: ClassVar[int]

    @abstractmethod
    def read_text(self, filename: str, data: bytes) -> tuple[str | None, bool]:
        """
        Extract the document's text.

        Args:
            filename: The uploaded name.
            data: The raw bytes, at most ``max_bytes + 1`` of them.

        Returns:
            ``(text, too_large)``, where ``text`` is None when there is none.
        """

    @abstractmethod
    def available(self, profile: Profile) -> bool:
        """Whether :meth:`extract_pins` may run for *profile* in this environment."""

    @abstractmethod
    def unavailable_warning(self, profile: Profile) -> str | None:
        """What to tell the uploader when a document with text is skipped because :meth:`available` is False, or None to say nothing."""

    @abstractmethod
    def extract_pins(self, filename: str, text: str, profile: Profile) -> tuple[dict[str, Any] | None, str | None]:
        """
        Turn the text into a pin list.

        Args:
            filename: The uploaded name, used for the list's display name.
            text: The text :meth:`read_text` returned.
            profile: The profile the import runs for.

        Returns:
            ``(list, warning)``.

        Raises:
            DocumentTooLargeError: The text is over the extractor's limit.
        """


def document_extractor_for(filename: str) -> DocumentExtractor | None:
    """The first extractor that handles *filename*'s extension, or None when no extractor does."""
    extension = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    for extractor in DOCUMENT_EXTRACTORS.items():
        if extension in extractor.extensions:
            return extractor
    return None


def document_extensions() -> frozenset[str]:
    """Every extension some extractor handles."""
    return frozenset().union(*(extractor.extensions for extractor in DOCUMENT_EXTRACTORS.items()))
