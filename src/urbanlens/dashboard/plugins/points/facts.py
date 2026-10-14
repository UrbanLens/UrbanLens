"""Facts: where a piece of evidence can come from."""

from __future__ import annotations

from urbanlens.dashboard.plugins.extensions import Choices

FACT_SOURCE_KINDS = Choices(
    "facts.source_kinds",
    "Kinds of source for a fact's evidence beyond core's ``FactSourceKind`` members, as ``value -> label``; a value is at most 17 characters. A plugin adds the kinds it records evidence under.",
)

__all__ = ["FACT_SOURCE_KINDS"]
