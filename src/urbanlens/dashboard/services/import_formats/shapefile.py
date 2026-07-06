"""Shapefile (.shp/.dbf/.shx bundle) pin import.

Shapefiles are always distributed as a set of same-stem sidecar files rather than
a single file, so unlike every other format here there is no single-file
"sniff and parse" step. Bundles must be grouped by filename stem *before* the
per-file format dispatch in the calling orchestration methods - see
``extract_shapefile_bundles()``, which is meant to be called once up front,
mirroring how ``import_pins_streaming`` already splits out Semantic Location
History files before its main per-file loop.

Only the ZIP-bundle upload path is supported: every real-world Shapefile source
(county GIS portals, EPA Envirofacts, data.gov, CalTopo) distributes a ``.zip``
containing the ``.shp``/``.dbf``/``.shx`` (and usually ``.prj``/``.cpg``) bundle,
and the existing archive extractor already unzips uploads.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import logging
from pathlib import Path
import tempfile
from typing import TYPE_CHECKING, Any

import geopandas
import pyogrio.errors

from urbanlens.dashboard.services.import_formats.heuristics import pick_name_and_description

if TYPE_CHECKING:
    from urbanlens.dashboard.models.profile import Profile

logger = logging.getLogger(__name__)

_SHAPEFILE_PART_EXTENSIONS = frozenset({"shp", "dbf", "shx", "prj", "cpg"})
_REQUIRED_PARTS = frozenset({"shp", "dbf"})


@dataclass
class ShapefileBundle:
    """The same-stem sidecar files that make up one Shapefile."""

    stem: str
    parts: dict[str, bytes] = field(default_factory=dict)


def _extension(filename: str) -> str:
    """Return the lowercase extension of *filename* without the leading dot."""
    return filename.rsplit(".", 1)[-1].lower() if "." in filename else ""


def _stem(filename: str) -> str:
    """Return the lowercase, extension-less basename of *filename*."""
    base = filename.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    return base.rsplit(".", 1)[0].lower() if "." in base else base.lower()


def extract_shapefile_bundles(files: list[tuple[str, bytes]]) -> tuple[list[ShapefileBundle], list[tuple[str, bytes]]]:
    """Split *files* into Shapefile bundles (grouped by stem) and everything else.

    Args:
        files: ``(filename, raw_bytes)`` pairs, e.g. already expanded from an
            uploaded ZIP archive.

    Returns:
        A ``(bundles, remaining_files)`` tuple. Bundles missing a required
        ``.shp`` or ``.dbf`` part are logged and dropped rather than passed
        through as an incomplete bundle that would only fail later.
    """
    grouped: dict[str, ShapefileBundle] = {}
    remaining: list[tuple[str, bytes]] = []

    for filename, data in files:
        ext = _extension(filename)
        if ext not in _SHAPEFILE_PART_EXTENSIONS:
            remaining.append((filename, data))
            continue
        stem = _stem(filename)
        bundle = grouped.setdefault(stem, ShapefileBundle(stem=stem))
        bundle.parts[ext] = data

    bundles: list[ShapefileBundle] = []
    for stem, bundle in grouped.items():
        missing = _REQUIRED_PARTS - bundle.parts.keys()
        if missing:
            logger.warning("Skipping incomplete shapefile bundle '%s': missing .%s", stem, ", .".join(sorted(missing)))
            continue
        bundles.append(bundle)

    return bundles, remaining


def shapefile_to_dict(bundle: ShapefileBundle, user_profile: Profile) -> list[dict[str, Any]]:
    """Convert one Shapefile bundle into pin dicts.

    Each feature's geometry centroid becomes a pin location (a no-op for Point
    features); the name/description are guessed from the attribute table via
    ``pick_name_and_description`` since column names vary by producer (and DBF
    column names are truncated to 10 characters, so exact matches can't be
    relied on).

    Args:
        bundle: The grouped sidecar files for one Shapefile.
        user_profile: The profile to associate with each pin.

    Returns:
        List of pin dicts, one per feature with a resolvable centroid.

    Raises:
        OSError: If the bundle can't be written to a temporary directory.
        ValueError: If GDAL rejects the bundle's geometry/attribute data.
        pyogrio.errors.DataSourceError: If GDAL cannot read the bundle as a Shapefile.
    """
    pins: list[dict[str, Any]] = []
    try:
        with tempfile.TemporaryDirectory(prefix="urbanlens_shp_") as tmp_dir:
            tmp_path = Path(tmp_dir)
            for ext, data in bundle.parts.items():
                (tmp_path / f"{bundle.stem}.{ext}").write_bytes(data)

            gdf = geopandas.read_file(tmp_path / f"{bundle.stem}.shp")
            if gdf.crs is not None and gdf.crs.to_epsg() != 4326:
                gdf = gdf.to_crs(epsg=4326)

            for _, row in gdf.iterrows():
                geometry = row.geometry
                if geometry is None or geometry.is_empty:
                    continue
                centroid = geometry.centroid
                if centroid.is_empty:
                    continue

                properties = row.drop(labels="geometry").to_dict()
                name, description = pick_name_and_description(properties, fallback_name=f"{bundle.stem} feature")
                pins.append(
                    {
                        "latitude": centroid.y,
                        "longitude": centroid.x,
                        "profile": user_profile,
                        "name": name,
                        "description": description,
                    },
                )

        logger.debug("Converted %s features from shapefile bundle '%s' to pins.", len(pins), bundle.stem)
    except (OSError, ValueError, pyogrio.errors.DataSourceError) as e:
        logger.exception("Failed to import pins from shapefile bundle '%s': %s", bundle.stem, e)
        raise

    return pins
