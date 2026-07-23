"""Build a markup-map snapshot from a PinList's pins.

Reuses the exact snapshot schema/materialization the rest of the app's
markup maps speak (``dashboard.services.map_snapshot.materialize_markup_map``)
so a list's markup map is a fully standard MarkupMap, editable through the
same shared composer as every other one.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from urbanlens.dashboard.models.pin_list.model import PinList


def build_list_markup_snapshot(pin_list: PinList) -> dict | None:
    """Build a markup snapshot dict with one pin marker per list item.

    Args:
        pin_list: The list to snapshot.

    Returns:
        A snapshot dict (see ``services.map_snapshot``), or None when the
        list has no pins with resolvable coordinates.
    """
    items = list(pin_list.items.select_related("pin__location").order_by("order"))
    located = [item for item in items if item.pin.location and item.pin.location.latitude is not None and item.pin.location.longitude is not None]
    if not located:
        return None

    center_lat = sum(float(item.pin.location.latitude) for item in located) / len(located)
    center_lng = sum(float(item.pin.location.longitude) for item in located) / len(located)
    return {
        "center_lat": center_lat,
        "center_lng": center_lng,
        "zoom": 12,
        "layer_mode": "street",
        "show_borders": False,
        "markup": [
            {
                "type": "pin",
                "latlngs": [[float(item.pin.location.latitude), float(item.pin.location.longitude)]],
                "label": item.pin.effective_name,
                "color": "#e53e3e",
            }
            for item in located
        ],
    }
