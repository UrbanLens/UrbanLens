"""
Beat builds its schedule at import, so the settings that schedule depends on must reach its container.

`celery-beat` gets a short explicit environment rather than the whole `.env` (it holds no API keys). A setting left
out is read as its default there: a plugin turned off in `UL_DISABLED_PLUGINS` kept its beat entries, and
`UL_BACKGROUND_TASKS_ALLOWLIST` never allowed anything.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from urbanlens.core.tests.testcase import SimpleTestCase
from urbanlens.UrbanLens.settings.app import AppSettings

COMPOSE = Path(__file__).resolve().parents[5] / "docker-compose.yml"

#: What `CELERY_BEAT_SCHEDULE` and the plugins' `BEAT_SCHEDULE` contributions are computed from, besides UL_ENVIRONMENT.
SCHEDULE_INPUTS = (
    "UL_ENVIRONMENT",
    "UL_BACKGROUND_TASKS_ALLOWLIST",
    "UL_ENVIRONMENT_SHARE",
    "UL_ENVIRONMENT_SHARE_OVERRIDES",
    "UL_DISABLED_PLUGINS",
    "UL_PLUGIN_MODULES",
)


class BeatReceivesScheduleSettingsTests(SimpleTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.beat_env = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))["services"]["celery-beat"]["environment"]

    def test_every_schedule_input_reaches_beat(self) -> None:
        for name in SCHEDULE_INPUTS:
            with self.subTest(name=name):
                self.assertIn(name, self.beat_env)

    def test_the_inputs_are_settings_the_app_reads(self) -> None:
        read = {field.validation_alias or f"UL_{key.upper()}" for key, field in AppSettings.model_fields.items()}
        for name in SCHEDULE_INPUTS:
            with self.subTest(name=name):
                self.assertIn(name, read)
