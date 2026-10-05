"""Base for pin-detail panels whose whole payload is one REData near-point call.
Eight plugins under ``plugins/builtin/redata_*.py`` had the same ``gate`` / ``fetch`` / ``debug_count`` skeleton copied into each of them, differing only in a gateway class, an accessor and the key the payload is stored under."""

from __future__ import annotations

from abc import abstractmethod
from typing import TYPE_CHECKING, ClassVar

from urbanlens.dashboard.services.pins.external_data import CoordinateGatedInfoPanelSource, PanelSource

if TYPE_CHECKING:
    from collections.abc import Sized

    from urbanlens.dashboard.models.pin.model import Pin
    from urbanlens.dashboard.services.apis.locations.redata_context_gateway import LocationContextEnvelope


def at_limit(rows: Sized, limit: int | None) -> bool:
    """Whether an answer filled the ``limit`` it was asked under, which makes any count drawn from it a floor.

    Args:
        rows: The rows the answer held.
        limit: The most rows asked for, or None for no limit.

    Returns:
        True when there may be more than ``rows``.
    """
    return limit is not None and len(rows) >= limit


def count_label(rows: Sized, limit: int | None) -> str:
    """How many rows an answer holds, said as a floor when it stopped at ``limit``.

    Args:
        rows: The rows the count describes.
        limit: The most rows asked for, or None for no limit.

    Returns:
        Such as ``"12"`` or ``"50+"``.
    """
    return f"{len(rows)}+" if at_limit(rows, limit) else str(len(rows))


class RedataBackedSource(PanelSource):
    """A panel source only REData can fill. Listed first among a source's bases, it adds to the source's own gate
    that REData is configured, so without REData the panel is never scheduled and caches nothing - an empty row
    written then would outlive REData being configured."""

    def gate(self, pin: Pin) -> bool:
        """REData is configured, and the source's own conditions hold.

        Args:
            pin: The pin whose panel is being rendered.

        Returns:
            True when a fetch is worth scheduling.
        """
        from urbanlens.dashboard.services.apis.locations.redata_context_gateway import redata_configured

        return redata_configured() and super().gate(pin)


class RedataInfoPanelSource(RedataBackedSource, CoordinateGatedInfoPanelSource):
    """An info panel filled by a single REData near-a-coordinate request.
    ``render_context`` remains the subclass's own, since turning one domain's rows into a card is the only genuinely per-panel code.

    Attributes:
        payload_key: The key the envelope's ``results`` are stored under in the ``LocationCache`` row, and read back from in ``render_context``.
        row_limit: Most rows this panel asks REData for, or None to take REData's own default. A cached answer holding
            this many is a first page, not a total."""

    payload_key: ClassVar[str]
    row_limit: ClassVar[int | None] = None

    def counted(self, rows: Sized) -> str:
        """How many rows an answer holds, said as a floor when the answer stopped at :attr:`row_limit`.

        Args:
            rows: The rows the count describes.

        Returns:
            Such as ``"12"`` or ``"50+"``.
        """
        return count_label(rows, self.row_limit)

    def is_full(self, rows: Sized) -> bool:
        """Whether an answer stopped at :attr:`row_limit`, so counts of its subsets are floors too.

        Args:
            rows: The rows the answer held.

        Returns:
            True when there may be more.
        """
        return at_limit(rows, self.row_limit)

    @abstractmethod
    def fetch_envelope(self, latitude: float, longitude: float) -> LocationContextEnvelope:
        """Make this panel's one REData call.

        Args:
            latitude: WGS-84 latitude of the pin.
            longitude: WGS-84 longitude of the pin.

        Returns:
            The parsed near-point envelope, whose ``complete`` flag decides whether the result may be cached.

        Raises:
            LocationContextUnavailableError: The request failed outright.
        """

    def transform_rows(self, rows: list[dict]) -> list[dict]:
        """Shape the envelope's rows before they are cached.

        Args:
            rows: The envelope's ``results``.

        Returns:
            The rows to store.
        """
        return rows

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
        data = {self.payload_key: self.transform_rows(envelope.results)}
        LocationCache.set(pin.location, self.cache_source, data, query_key=f"{latitude:.5f},{longitude:.5f}")
        self.landed(pin, data)

    def landed(self, pin: Pin, data: dict) -> None:
        """Do whatever else a fetched answer calls for, once it is cached.

        Args:
            pin: The pin whose panel was fetched.
            data: The payload just cached.
        """

    def debug_count(self, data: dict) -> int:
        """Number of rows cached, for the admin debug overlay."""
        return len((data or {}).get(self.payload_key) or [])


__all__ = ["RedataBackedSource", "RedataInfoPanelSource"]
