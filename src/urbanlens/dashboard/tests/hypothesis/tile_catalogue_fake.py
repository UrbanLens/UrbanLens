"""A stand-in tile catalogue, so tests of the basemap proxy and catalogue need no plugin."""

from __future__ import annotations

from contextlib import AbstractContextManager
from typing import Any, ClassVar

from urbanlens.dashboard.plugins.points import TILE_CATALOGUES
from urbanlens.dashboard.services.map.basemap_catalogue import TileCatalogue

#: Answered to a tile request by a fake that was given no tile, which is a test reaching a layer it should not have.
UNEXPECTED_FETCH = (599, b"unexpected", "text/plain")


class FakeTileCatalogue(TileCatalogue):
    """
    A catalogue that publishes what it is given and serves what it is told to.

    Attributes:
        sources: What :meth:`list_sources` answers. Set ``list_error`` to make it raise instead.
        tile: What :meth:`download_tile` answers; ``tile_error`` makes it raise.
        sources_calls: How many times the service was asked for its layers.
        tile_calls: The ``(layer, z, x, y)`` of every tile fetched.
        vector_refusal: The body :meth:`refuses_as_vector` recognises on a 400.
    """

    cache_tag: ClassVar[str] = "fake"
    vector_refusal: ClassVar[bytes] = b"vector_layer_not_served"

    def __init__(
        self, sources: list[dict[str, Any]] | None = None, *, tile: tuple[int, bytes, str] = UNEXPECTED_FETCH
    ) -> None:
        self.sources = sources if sources is not None else []
        self.tile = tile
        self.list_error: Exception | None = None
        self.tile_error: Exception | None = None
        self.sources_calls = 0
        self.tile_calls: list[tuple[str, int, int, int]] = []

    def available(self) -> bool:
        """Always: a test that wants none holds an empty seam instead."""
        return True

    def list_sources(self) -> list[dict[str, Any]]:
        """The layers this fake was given."""
        self.sources_calls += 1
        if self.list_error is not None:
            raise self.list_error
        return [dict(source) for source in self.sources]

    def download_tile(self, layer: str, z: int, x: int, y: int) -> tuple[int, bytes, str]:
        """The tile this fake was given."""
        self.tile_calls.append((layer, z, x, y))
        if self.tile_error is not None:
            raise self.tile_error
        return self.tile

    def refuses_as_vector(self, status: int, body: bytes) -> bool:
        """Whether this is a 400 saying the layer is a vector style."""
        return status == 400 and self.vector_refusal in body


def catalogue_on(catalogue: TileCatalogue | None = None) -> AbstractContextManager[None]:
    """The seam holding only ``catalogue`` (a fresh fake by default) for the block."""
    return TILE_CATALOGUES.overridden([catalogue or FakeTileCatalogue()])


def catalogue_off() -> AbstractContextManager[None]:
    """The seam holding no catalogue for the block: an install with none."""
    return TILE_CATALOGUES.overridden([])


class FakeCatalogueMixin:
    """Holds a fresh :class:`FakeTileCatalogue` in the seam for each test, as ``self.catalogue``."""

    def setUp(self) -> None:
        super().setUp()
        self.catalogue = FakeTileCatalogue()
        self.enterContext(catalogue_on(self.catalogue))
