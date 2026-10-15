"""Template tags for the import pages' file-type hints."""

from __future__ import annotations

from typing import TYPE_CHECKING

from django import template

if TYPE_CHECKING:
    from urbanlens.dashboard.models.profile.model import Profile

register = template.Library()


@register.simple_tag
def document_import_extensions(profile: Profile) -> list[str]:
    """
    The dotted document extensions this profile may import right now, for an upload's ``accept`` list.

    Args:
        profile: The profile importing.

    Returns:
        The extensions of every document extractor that is available to *profile*, sorted.
    """
    from urbanlens.dashboard.plugins.points import DOCUMENT_EXTRACTORS

    return sorted({f".{extension}" for extractor in DOCUMENT_EXTRACTORS.items() if extractor.available(profile) for extension in extractor.extensions})
