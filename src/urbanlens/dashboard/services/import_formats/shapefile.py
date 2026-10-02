"""Shapefile (.shp/.dbf/.shx bundle) pin import.
Shapefiles are always distributed as a set of same-stem sidecar files rather than a single file, so unlike every other format here there is no single-file "sniff and parse" step."""

from __future__ import annotations

import datetime
import logging
from pathlib import Path
import shutil
from typing import IO, TYPE_CHECKING, Any

import geopandas
import pyogrio
import pyogrio.errors
from pyogrio.raw import open_arrow

from urbanlens.dashboard.services.import_formats.heuristics import pick_name_and_description
from urbanlens.dashboard.services.import_formats.streams import READ_CHUNK_BYTES
from urbanlens.dashboard.services.sandbox import untrusted_parse

if TYPE_CHECKING:
    from collections.abc import Iterator

    from urbanlens.dashboard.models.profile import Profile

logger = logging.getLogger(__name__)

_SHAPEFILE_PART_EXTENSIONS = frozenset({"shp", "dbf", "shx", "prj", "cpg"})
_REQUIRED_PARTS = frozenset({"shp", "dbf"})
_BATCH_FEATURES = 1000


def _extension(filename: str) -> str:
    """Return the lowercase extension of *filename* without the leading dot."""
    return filename.rsplit(".", 1)[-1].lower() if "." in filename else ""


def _stem(filename: str) -> str:
    """Return the lowercase, extension-less basename of *filename*."""
    base = filename.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    return base.rsplit(".", 1)[0].lower() if "." in base else base.lower()


def is_shapefile_part(filename: str) -> bool:
    """Whether *filename* is one of the sidecar files a Shapefile bundle is made of.

    Args:
        filename: The file's name.

    Returns:
        True for ``.shp``, ``.dbf``, ``.shx``, ``.prj`` and ``.cpg``.
    """
    return _extension(filename) in _SHAPEFILE_PART_EXTENSIONS


class ShapefileSpool:
    """Shapefile sidecar parts copied to disk as they arrive, grouped by stem, so none is held in memory.

    A later part with the same stem and extension replaces an earlier one.
    """

    def __init__(self, directory: str) -> None:
        """Spool into *directory*.

        Args:
            directory: Somewhere on disk, empty, and removed by the caller.
        """
        self.directory = Path(directory)
        self._parts: dict[str, set[str]] = {}

    def add(self, filename: str, stream: IO[bytes]) -> None:
        """Copy one part to disk.

        Args:
            filename: The part's name, whose stem groups it and whose extension says which part it is.
            stream: The part's content.
        """
        stem = _stem(filename)
        exts = self._parts.setdefault(stem, set())
        ext = _extension(filename)
        exts.add(ext)
        with (self._base(stem).with_suffix(f".{ext}")).open("wb") as out:
            shutil.copyfileobj(stream, out, READ_CHUNK_BYTES)

    def bundles(self) -> Iterator[tuple[str, Path]]:
        """Each complete bundle, in the order its first part arrived.

        Yields:
            The bundle's stem and its ``.shp`` file, beside the other parts under the same name.
        """
        for stem, exts in self._parts.items():
            missing = _REQUIRED_PARTS - exts
            if missing:
                logger.warning("Skipping incomplete shapefile bundle '%s': missing .%s", stem, ", .".join(sorted(missing)))
                continue
            yield stem, self._base(stem).with_suffix(".shp")

    def _base(self, stem: str) -> Path:
        # Named by arrival rather than by stem, which is the uploader's to choose.
        return self.directory / f"bundle-{list(self._parts).index(stem)}"


@untrusted_parse("geo.shapefile")
def iter_shapefile_pins(shp_path: Path, stem: str, user_profile: Profile) -> Iterator[dict[str, Any]]:
    """Read a Shapefile on disk into pin dicts, a batch of features at a time.

    GDAL's Arrow stream is a single cursor over the features; skipping to an offset instead would count the
    records a ``.dbf`` marks deleted, which GDAL does not return. A null attribute is left out, as an absent
    one is, and a Date reads as a timestamp at midnight.

    Text GDAL cannot recode to UTF-8 itself - a ``.dbf`` with neither a ``.cpg`` nor a code page byte - is
    decoded as the encoding pyogrio reports for it, ISO-8859-1, which the stream does not do unasked.

    Args:
        shp_path: The ``.shp`` file, with its sidecar parts beside it under the same name.
        stem: The bundle's name, for features without one of their own.
        user_profile: The profile to associate with each pin.

    Yields:
        One pin dict per feature with a resolvable centroid.

    Raises:
        ValueError: If GDAL rejects the bundle's geometry/attribute data.
        OSError: If GDAL fails partway through reading it.
        pyogrio.errors.DataSourceError: If GDAL cannot read the bundle as a Shapefile."""
    encoding = pyogrio.read_info(shp_path)["encoding"]
    recode = None if encoding.upper() == "UTF-8" else encoding
    with open_arrow(shp_path, encoding=recode, batch_size=_BATCH_FEATURES, use_pyarrow=True) as (meta, batches):
        geometry_column = meta["geometry_name"] or "wkb_geometry"
        for batch in batches:
            geometries = geopandas.GeoSeries.from_wkb(batch.column(geometry_column).to_numpy(zero_copy_only=False), crs=meta["crs"])
            if geometries.crs is not None and geometries.crs.to_epsg() != 4326:
                geometries = geometries.to_crs(epsg=4326)
            rows = batch.drop_columns([geometry_column]).to_pylist()
            for geometry, row in zip(geometries, rows, strict=True):
                if geometry is None or geometry.is_empty:
                    continue
                centroid = geometry.centroid
                if centroid.is_empty:
                    continue

                properties = {key: _attribute(value) for key, value in row.items() if value is not None}
                name, description = pick_name_and_description(properties, fallback_name=f"{stem} feature")
                yield {
                    "latitude": centroid.y,
                    "longitude": centroid.x,
                    "profile": user_profile,
                    "name": name,
                    "description": description,
                }


def _attribute(value: object) -> object:
    if type(value) is datetime.date:
        return datetime.datetime.combine(value, datetime.time())
    return value
