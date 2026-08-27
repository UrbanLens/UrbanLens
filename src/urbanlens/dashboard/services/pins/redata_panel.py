"""Base for pin-detail panels whose whole payload is one REData near-point call.

Eight plugins under ``plugins/builtin/redata_*.py`` had the same ``gate`` /
``fetch`` / ``debug_count`` skeleton copied into each of them, differing only in
a gateway class, an accessor and the key the payload is stored under. Copying a
skeleton means every new panel is a fresh chance to omit a line, and one already
had: ``redata_building_attributes`` shipped without the ``redata_configured()``
half of its gate.

Two things therefore live here rather than in each panel.

**The gate**, so a panel whose only source is REData cannot be scheduled on an
install that has no REData.

**The rule about outages**, which is the part that was actually wrong. REData's
near-point envelope carries ``complete: false`` when a source covering the
coordinate could not be reached, and its own contract says such a response must
never be cached as emptiness (``../REData/docs/api-reference.md``, the
``providers`` block: "Retryable; never cache this as emptiness"). Every panel
parsed that field and threw it away, then wrote the empty list to
``LocationCache`` - and the existence of a row is what marks a source as
fetched, so a five-minute outage blanked a panel for the whole
``external_data_cache_days`` window with nothing to retry it. This is the same
defect ``tests/hypothesis/test_outage_not_cached_as_empty.py`` was written for,
one level finer: it guarded a failed *request*, and this is a failed *provider*
inside a successful request.

The distinction that rule keeps is the one that test states: *asked and told
nothing* is a result worth caching; *could not ask* is not. An incomplete
response that still carries rows was asked and answered - it is cached, because
refusing to would mean never caching anywhere one flaky provider is down.

This does not subsume the panels that combine several calls
(``redata_site_conditions``) or that are gallery rather than info sources
(``redata_aerial_media``); those keep their own ``fetch``.
"""

from __future__ import annotations

from abc import abstractmethod
from typing import TYPE_CHECKING, ClassVar

from urbanlens.dashboard.services.pins.external_data import CoordinateGatedInfoPanelSource

if TYPE_CHECKING:
    from urbanlens.dashboard.models.pin.model import Pin
    from urbanlens.dashboard.services.apis.locations.redata_context_gateway import LocationContextEnvelope


class RedataInfoPanelSource(CoordinateGatedInfoPanelSource):
    """An info panel filled by a single REData near-a-coordinate request.

    A subclass declares :attr:`payload_key` and implements
    :meth:`fetch_envelope`; everything else - the REData gate, the cache write,
    the outage rule and the debug count - is inherited. ``render_context``
    remains the subclass's own, since turning one domain's rows into a card is
    the only genuinely per-panel code.

    Attributes:
        payload_key: The key the envelope's ``results`` are stored under in the
            ``LocationCache`` row, and read back from in ``render_context``.
    """

    payload_key: ClassVar[str]

    @abstractmethod
    def fetch_envelope(self, latitude: float, longitude: float) -> LocationContextEnvelope:
        """Make this panel's one REData call.

        Args:
            latitude: WGS-84 latitude of the pin.
            longitude: WGS-84 longitude of the pin.

        Returns:
            The parsed near-point envelope, whose ``complete`` flag decides
            whether the result may be cached.

        Raises:
            LocationContextUnavailableError: The request failed outright. Left
                to propagate - the panel-fetch machinery already treats a raise
                as "not fetched", which is what leaves the source retryable.
        """

    def transform_rows(self, rows: list[dict]) -> list[dict]:
        """Shape the envelope's rows before they are cached. Identity by default.

        For a panel that stores less than REData returned - dropping a geometry
        it never draws, say - so the narrowing stays next to the panel that
        needs it rather than becoming a reason to re-implement :meth:`fetch`.

        Args:
            rows: The envelope's ``results``.

        Returns:
            The rows to store.
        """
        return rows

    def gate(self, pin: Pin) -> bool:
        """Also requires REData to be configured - these panels have no other source."""
        from urbanlens.dashboard.services.apis.locations.redata_context_gateway import redata_configured

        return super().gate(pin) and redata_configured()

    def fetch(self, pin: Pin) -> None:
        """Call REData and cache the rows, unless the answer is an outage.

        Args:
            pin: The pin whose location is being filled.
        """
        from urbanlens.dashboard.models.cache.location_cache import LocationCache

        latitude = float(pin.effective_latitude or 0)
        longitude = float(pin.effective_longitude or 0)
        envelope = self.fetch_envelope(latitude, longitude)
        if not envelope.complete and not envelope.results:
            # Nothing came back and REData says that is because a source
            # covering this point could not be reached. Writing the row would
            # record the outage as a settled "nothing here" for the whole
            # cache window; leaving it absent is what makes it retryable.
            return
        LocationCache.set(pin.location, self.cache_source, {self.payload_key: self.transform_rows(envelope.results)}, query_key=f"{latitude:.5f},{longitude:.5f}")

    def debug_count(self, data: dict) -> int:
        """Number of rows cached, for the admin debug overlay."""
        return len((data or {}).get(self.payload_key) or [])


__all__ = ["RedataInfoPanelSource"]
