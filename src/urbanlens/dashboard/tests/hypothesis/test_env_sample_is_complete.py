"""Every Settings field's environment variable is documented in ``.env-sample``."""

from __future__ import annotations

from pathlib import Path
import re

from pydantic.fields import FieldInfo

from urbanlens.core.tests.testcase import SimpleTestCase
from urbanlens.UrbanLens.settings.app import AppSettings

# repo/src/urbanlens/dashboard/tests/hypothesis/this_file.py
ENV_SAMPLE = Path(__file__).resolve().parents[5] / ".env-sample"

_DOCUMENTED = re.compile(r"^#?\s*([A-Z][A-Z0-9_]*)=", re.MULTILINE)


def _env_name(name: str, field: FieldInfo) -> str:
    """Return the variable pydantic-settings reads for ``field``: its alias if it has one, else ``UL_`` + the name."""
    alias = field.validation_alias if isinstance(field.validation_alias, str) else field.alias
    return alias or f"UL_{name.upper()}"


class EnvSampleCompletenessTests(SimpleTestCase):
    """A new Settings field must come with a line in .env-sample, active or commented out."""

    def test_every_settings_field_is_documented(self) -> None:
        documented = set(_DOCUMENTED.findall(ENV_SAMPLE.read_text(encoding="utf-8")))
        missing = {
            name: _env_name(name, field)
            for name, field in AppSettings.model_fields.items()
            if _env_name(name, field) not in documented
        }

        self.assertEqual(missing, {}, f"Add these to .env-sample (field -> variable): {missing}")
