"""
Layers a plugin adds to the Private Pin page's map.

A layer is a button in the layers panel plus a JSON endpoint the page fetches once the button is switched on. The
endpoint answers a GeoJSON ``FeatureCollection`` that carries its own presentation, so no layer needs script of its own:

- each feature's ``properties`` holds ``color`` (an outline colour), ``label`` (the resting-pointer heading) and ``name``;
- ``notice`` (optional) is ``{"kind": "info" | "warning", "message": ...}``, shown as a toast when the layer loads.

The layer's own ``attribution`` is credited under the map while it is on.

An endpoint that cannot answer for now replies 503 with ``{"error": message}`` and ``Retry-After``.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any, ClassVar

if TYPE_CHECKING:
    from urbanlens.dashboard.models.pin.model import Pin
    from urbanlens.dashboard.templatetags.map_components import MapLayerSpec


class PinMapLayer(ABC):
    """
    One layer a plugin offers on a pin's map, contributed to ``PIN_MAP_LAYERS``.

    Attributes:
        spec: The layers-panel button. Its ``kind`` is ``custom``, and its ``key`` names the layer to the page.
        attribution: Credit shown under the map while the layer is on, or empty.
    """

    spec: ClassVar[MapLayerSpec]
    attribution: ClassVar[str] = ""

    @abstractmethod
    def offered(self, pin: Pin) -> bool:
        """Whether the pin's map has anything this layer could draw."""

    @abstractmethod
    def data_url(self, pin: Pin) -> str:
        """The pin's JSON endpoint for this layer."""


def offered_pin_layers(pin: Pin) -> list[PinMapLayer]:
    """The plugin layers the pin's map offers, in priority order."""
    from urbanlens.dashboard.plugins.points import PIN_MAP_LAYERS

    return [layer for layer in PIN_MAP_LAYERS if layer.offered(pin)]


def pin_layers_config(pin: Pin, layers: list[PinMapLayer]) -> list[dict[str, Any]]:
    """What the page's script needs to draw ``layers``, as returned by :func:`offered_pin_layers`."""
    return [{"key": layer.spec.key, "url": layer.data_url(pin), "attribution": layer.attribution} for layer in layers]
