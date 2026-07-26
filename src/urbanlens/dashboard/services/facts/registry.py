"""Extensible registry of known Fact keys - the single place a new fact key is declared.

Mirrors ``services.consensus.fields``'s ``ConsensusFieldStrategy`` registry
and ``services.spotguessr.modes``'s ``ModeStrategy`` registry: adding a new
fact key means adding one ``FactKeyDefinition`` here (or, for a future
plugin-contributed key, calling ``register()`` at import time), never
touching ``Fact.key``'s schema - it is a plain, unconstrained ``CharField``
(see ``models.facts.model``), specifically so new keys never require a
migration.
"""

from __future__ import annotations

from dataclasses import dataclass

from urbanlens.dashboard.models.facts.model import FactDataType, FactSubjectType


@dataclass(frozen=True)
class FactKeyDefinition:
    """Everything key-specific about validating and displaying one kind of Fact.

    Attributes:
        key: The ``Fact.key``/``FactEvidence`` value this definition governs.
        data_type: Which ``FactDataType`` values recorded under this key are
            stored/compared as.
        allowed_subject_types: Which ``FactSubjectType`` values this key may
            attach to - e.g. ``photo_coordinates`` is Image-only.
        display_name: Human-readable label, for admin/debug UI and AI prompts.
    """

    key: str
    data_type: str
    allowed_subject_types: frozenset[str]
    display_name: str


_REGISTRY: dict[str, FactKeyDefinition] = {}


def register(definition: FactKeyDefinition) -> None:
    """Register (or replace) a fact-key definition - the extension seam for future/plugin keys."""
    _REGISTRY[definition.key] = definition


def get_definition(key: str) -> FactKeyDefinition | None:
    """The registered definition for ``key``, or None if it isn't registered."""
    return _REGISTRY.get(key)


def all_keys() -> list[str]:
    """Every registered fact key."""
    return list(_REGISTRY.keys())


def _register_defaults() -> None:
    register(FactKeyDefinition("photo_coordinates", FactDataType.POINT, frozenset({FactSubjectType.IMAGE}), "Photo location"))
    register(FactKeyDefinition("wiki_name", FactDataType.TEXT, frozenset({FactSubjectType.WIKI}), "Place name"))
    register(FactKeyDefinition("wiki_description", FactDataType.TEXT, frozenset({FactSubjectType.WIKI}), "Description"))
    register(FactKeyDefinition("wiki_indoor_outdoor", FactDataType.CHOICE, frozenset({FactSubjectType.WIKI}), "Indoor/Outdoor"))
    register(FactKeyDefinition("wiki_pin_type", FactDataType.CHOICE, frozenset({FactSubjectType.WIKI}), "Place type"))
    register(FactKeyDefinition("built_year", FactDataType.NUMBER, frozenset({FactSubjectType.LOCATION, FactSubjectType.WIKI}), "Year built"))


_register_defaults()
