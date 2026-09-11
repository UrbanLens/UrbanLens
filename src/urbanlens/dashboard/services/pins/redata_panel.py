"""Base for pin-detail panels whose whole payload is one REData near-point call.
Eight plugins under ``plugins/builtin/redata_*.py`` had the same ``gate`` / ``fetch`` / ``debug_count`` skeleton copied into each of them, differing only in a gateway class, an accessor and the key the payload is stored under."""

from __future__ import annotations

from abc import abstractmethod
from typing import TYPE_CHECKING, ClassVar

from urbanlens.dashboard.services.pins.external_data import CoordinateGatedInfoPanelSource

if TYPE_CHECKING:
    from urbanlens.dashboard.models.pin.model import Pin
    from urbanlens.dashboard.services.apis.locations.redata_context_gateway import LocationContextEnvelope


class RedataInfoPanelSource(CoordinateGatedInfoPanelSource):
    """An info panel filled by a single REData near-a-coordinate request.
    ``render_context`` remains the subclass's own, since turning one domain's rows into a card is the only genuinely per-panel code.

    Attributes:
        payload_key: The key the envelope's ``results`` are stored under in the
            ``LocationCache`` row, and read back from in ``render_context``."""

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
                as "not fetched", which is what leaves the source retryable."""

    def transform_rows(self, rows: list[dict]) -> list[dict]:
        """Shape the envelope's rows before they are cached. Identity by default.

        Args:
                rows: The envelope's ``results``.

        Returns:
                The rows to store."""
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
            # Nothing came back and REData says that is because a source covering this point could
            # not be reached.
            # Writing the row would record the outage as a settled "nothing here" for the whole
            # cache window; leaving it absent is what makes it retryable.
            return
        LocationCache.set(pin.location, self.cache_source, {self.payload_key: self.transform_rows(envelope.results)}, query_key=f"{latitude:.5f},{longitude:.5f}")

    def debug_count(self, data: dict) -> int:
        """Number of rows cached, for the admin debug overlay."""
        return len((data or {}).get(self.payload_key) or [])


__all__ = ["RedataInfoPanelSource"]
