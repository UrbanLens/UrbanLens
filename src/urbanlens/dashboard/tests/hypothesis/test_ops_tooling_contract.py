"""The `infrastructure` repo's staging pipeline names this repo's table names."""

from __future__ import annotations

import os
from pathlib import Path
import sys

from urbanlens.core.tests.testcase import SimpleTestCase


def _infra_opslib_dir() -> Path | None:
    """Locate the sibling ``infrastructure`` checkout's ``bin/opslib``.

    Defaults to a directory named ``infrastructure`` next to this checkout's root (this host's actual layout:
    ``/projects/UrbanLens/{UrbanLens, infrastructure}``), overridable via ``UL_INFRA_DIR`` for any other.

    Returns:
        The directory containing ``opslib``, or ``None`` if it isn't there."""
    override = os.getenv("UL_INFRA_DIR")
    candidates = [Path(override)] if override else []
    for parent in Path(__file__).resolve().parents:
        if (parent / ".git").is_dir():
            candidates.append(parent.parent / "infrastructure")
            break
    for candidate in candidates:
        bin_dir = candidate / "bin"
        if (bin_dir / "opslib" / "__init__.py").is_file():
            return bin_dir
    return None


_INFRA_BIN = _infra_opslib_dir()


class VerifiedTablesTests(SimpleTestCase):
    """A table name that matches nothing counts -1, which the check skipped."""

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        if _INFRA_BIN is None:
            raise __import__("unittest").SkipTest(
                "infrastructure repo not found (expected a sibling 'infrastructure' checkout, or UL_INFRA_DIR set) - "
                "cannot check opslib/staging.py's _VERIFIED_TABLES without it"
            )
        if str(_INFRA_BIN) not in sys.path:
            sys.path.insert(0, str(_INFRA_BIN))

    def test_every_verified_table_exists_in_the_schema(self) -> None:
        from django.apps import apps
        from opslib.staging import _VERIFIED_TABLES

        known = {model._meta.db_table for model in apps.get_models()}

        missing = [table for table in _VERIFIED_TABLES if table not in known]

        self.assertEqual(
            missing, [], "a name no model owns can never be counted, so it silently drops out of the comparison"
        )

    def test_the_pins_table_is_named_the_way_the_model_names_it(self) -> None:
        """`dashboard_pins` was checked for a year and exists nowhere."""
        from opslib.staging import _VERIFIED_TABLES

        from urbanlens.dashboard.models.pin.model import Pin

        self.assertIn(Pin._meta.db_table, _VERIFIED_TABLES)
