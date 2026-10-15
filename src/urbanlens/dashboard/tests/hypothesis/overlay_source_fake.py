"""A stand-in overlay source for georeferenced map sheets, so tests of overlay import need no plugin."""

from __future__ import annotations

from contextlib import AbstractContextManager
import re
from typing import ClassVar
import uuid

from urbanlens.dashboard.plugins.points import OVERLAY_SOURCES
from urbanlens.dashboard.services.map.image_overlays import OverlaySource

_OWN = re.compile(r"/dashboard/fake-sheets/(?P<id>[0-9a-fA-F-]{36})/\{z\}/\{x\}/\{y\}\.png")
_UPSTREAM = re.compile(r"/upstream/(?P<id>[0-9a-fA-F-]{36})/tiles/\{z\}/\{x\}/\{y\}\.png")


def fake_sheet_template(sheet: str) -> str:
    """This site's tile template for one sheet of the fake source."""
    return f"/dashboard/fake-sheets/{sheet}/{{z}}/{{x}}/{{y}}.png"


class FakeSheetSource(OverlaySource):
    """Recognises ``/dashboard/fake-sheets/<uuid>/...`` and ``/upstream/<uuid>/tiles/...`` as the same sheet."""

    heading: ClassVar[str] = "Fake sheets covering this location"

    def available(self) -> bool:
        """Always."""
        return True

    def browse_url(self, owner: object) -> str:
        """A fixed path."""
        return "/dashboard/fake-sheets/"

    def recognized_path(self, path: str) -> str | None:
        """This site's route for the sheet the path names."""
        match = _OWN.fullmatch(path) or _UPSTREAM.fullmatch(path)
        return fake_sheet_template(str(uuid.UUID(match["id"]))) if match else None


def fake_sheet_source(*, none: bool = False) -> AbstractContextManager[None]:
    """The seam holding only the fake source for the block, or nothing when ``none``."""
    return OVERLAY_SOURCES.overridden([] if none else [FakeSheetSource()])
