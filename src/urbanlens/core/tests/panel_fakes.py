"""Panel sources that make no upstream calls, for tests of the panel machinery that should not depend on a plugin."""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from urbanlens.dashboard.plugins.points import ENRICHMENT_SOURCES, OVERTURE_MIRRORS, PANEL_SOURCES
from urbanlens.dashboard.plugins.registry import plugin_registry
from urbanlens.dashboard.services.pins.external_data import InfoPanelSource

if TYPE_CHECKING:
    from urbanlens.core.tests.testcase import TestCase
    from urbanlens.dashboard.models.pin.model import Pin
    from urbanlens.dashboard.plugins.extensions import Seam


class FakeInfoPanel(InfoPanelSource):
    """
    A cache-backed info panel whose fetch does nothing and whose content test is "has a ``rows`` list".

    Subclass it (see :func:`info_panel`) to set ``key``, ``cache_max_age``, ``site_level`` and the rest.
    """

    key: ClassVar[str] = "fake_info"
    cache_source: ClassVar[str] = "fake_info"
    section_id: ClassVar[str] = "fake-info-section"
    title: ClassVar[str] = "Fake"

    def fetch(self, pin: Pin) -> None:
        return None

    def has_content(self, data: dict | None) -> bool:
        return bool((data or {}).get("rows"))

    def render_context(self, pin: Pin, data: dict) -> dict | None:
        rows = (data or {}).get("rows") or []
        return {"facts": [{"icon": "info", "text": str(row)} for row in rows]} if rows else None


def info_panel(key: str = "fake_info", **attributes: object) -> FakeInfoPanel:
    """
    An instance of a :class:`FakeInfoPanel` subclass with ``attributes`` set as class attributes.

    Args:
        key: The panel's key; also its ``cache_source`` unless ``attributes`` says otherwise.
    """
    namespace = {"key": key, "cache_source": key, "section_id": f"{key}-section", **attributes}
    return type(f"FakeInfoPanel_{key}", (FakeInfoPanel,), namespace)()


def hold_panels(test: TestCase, *sources: InfoPanelSource) -> None:
    """Register ``sources`` as panel sources for the rest of ``test``, alongside the installed ones."""
    context = PANEL_SOURCES.overridden([*PANEL_SOURCES, *sources])
    context.__enter__()
    test.addCleanup(context.__exit__, None, None, None)


def hold_bundled_sources(test: TestCase) -> None:
    """
    For the rest of ``test``, keep only the panel sources, enrichment sources and Overture mirrors that core and
    its bundled plugins contribute.

    A sweep of "every registered source" then covers the same sources whichever package plugins are installed and
    configured; a plugin that wants its own sources swept runs the sweep with its settings in place.
    """
    _hold_bundled(test, PANEL_SOURCES)
    _hold_bundled(test, ENRICHMENT_SOURCES)
    _hold_bundled(test, OVERTURE_MIRRORS)


def _hold_bundled[T](test: TestCase, seam: Seam[T]) -> None:
    bundled = [contribution.value for contribution in seam.contributions() if _is_bundled(contribution.owner)]
    context = seam.overridden(bundled)
    context.__enter__()
    test.addCleanup(context.__exit__, None, None, None)


def _is_bundled(owner: str | None) -> bool:
    if owner is None:
        return True
    info = plugin_registry.info(owner)
    return info is not None and not info.source.is_package
