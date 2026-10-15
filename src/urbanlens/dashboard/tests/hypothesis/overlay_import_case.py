"""A profile with a pin, ready to import a map overlay onto."""

from __future__ import annotations

import shutil
import tempfile
from unittest.mock import patch

from django.contrib.auth.models import User
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.map_overlay.model import MapImageOverlay
from urbanlens.dashboard.services.import_export import import_data

CORNERS = [[40.002, -74.002], [40.002, -74.000], [40.000, -74.000], [40.000, -74.002]]
PUBLIC_DNS_RESULT = [(2, 1, 6, "", ("93.184.216.34", 0))]


class ImportCase(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.profile = baker.make(User).profile
        self.pin = baker.make_recipe("dashboard.pin", profile=self.profile)
        self.data_dir = tempfile.mkdtemp(prefix="ul_tile_import_")
        self.addCleanup(shutil.rmtree, self.data_dir, ignore_errors=True)
        self.ctx = import_data.ImportContext(
            profile=self.profile,
            data_dir=self.data_dir,
            result=import_data.ImportResult(),
            pin_uuid_map={str(self.pin.uuid): self.pin.pk},
            label_uuid_map={},
        )

    def _import(self, template: str) -> MapImageOverlay | None:
        row = {
            "name": "Sanborn",
            "corners": CORNERS,
            "target_type": "pin",
            "target_uuid": str(self.pin.uuid),
            "tile_url_template": template,
        }
        with patch("socket.getaddrinfo", return_value=PUBLIC_DNS_RESULT):
            import_data.MapAnnotationsImport()._import_overlay(row, self.ctx, 1)
        return MapImageOverlay.objects.for_pin(self.pin).first()
