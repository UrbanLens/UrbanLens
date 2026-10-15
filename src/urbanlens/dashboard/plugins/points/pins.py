"""Pin merges: what a plugin that keeps rows of its own about a pin carries across when another pin absorbs it."""

from __future__ import annotations

from typing import TYPE_CHECKING

from urbanlens.dashboard.plugins.extensions import ActionHook

if TYPE_CHECKING:
    from urbanlens.dashboard.models.pin.model import Pin


PIN_ABSORBING: ActionHook[[Pin, Pin]] = ActionHook(
    "pins.absorbing",
    "Fired with (survivor, loser) after core has moved its own rows and before the loser is deleted. A callback moves its rows that refer to the loser onto the survivor. Raises, so a merge never deletes a pin whose rows were not carried over.",
    on_error="raise",
)

__all__ = ["PIN_ABSORBING"]
