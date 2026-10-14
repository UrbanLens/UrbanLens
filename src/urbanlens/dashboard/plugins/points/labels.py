"""Labels: how a label being created gets its presentation."""

from __future__ import annotations

from typing import TYPE_CHECKING

from urbanlens.dashboard.plugins.extensions import ExtensionPoint

if TYPE_CHECKING:
    from urbanlens.dashboard.services.labels.style_suggestions import LabelStyleSuggester


LABEL_STYLE_SUGGESTERS: ExtensionPoint[LabelStyleSuggester] = ExtensionPoint(
    "labels.style_suggesters",
    "Choosers of a new label's icon and colour, asked in priority order; the first to return a suggestion decides, and with none the label keeps its default style.",
)

__all__ = ["LABEL_STYLE_SUGGESTERS"]
