"""Tests for services.device_scan.type_guessing."""

from __future__ import annotations

from django.test import SimpleTestCase

from urbanlens.dashboard.models.device_scan.model import DeviceType
from urbanlens.dashboard.services.device_scan.type_guessing import guess_device_type


class GuessDeviceTypeTests(SimpleTestCase):
    """Pure name/OUI heuristic; when it applies is test_device_scan_records.py's business."""

    def test_name_substring_match_is_case_insensitive(self) -> None:
        device_type, confidence = guess_device_type(mac_address="00:00:00:00:00:00", display_name="Wyze Cam v3")
        self.assertEqual(device_type, DeviceType.CAMERA)
        self.assertGreater(confidence, 0)

    def test_tracker_name_match(self) -> None:
        device_type, _confidence = guess_device_type(mac_address="00:00:00:00:00:00", display_name="Someone's AirTag")
        self.assertEqual(device_type, DeviceType.TRACKER)

    def test_oui_match_when_name_is_blank(self) -> None:
        device_type, confidence = guess_device_type(mac_address="8C:C8:F4:11:22:33", display_name="")
        self.assertEqual(device_type, DeviceType.CAMERA)
        self.assertGreater(confidence, 0)

    def test_name_match_wins_over_oui_when_both_present(self) -> None:
        """A Hikvision OUI (camera) paired with a name that matches a tracker: name wins."""
        device_type, _confidence = guess_device_type(mac_address="8C:C8:F4:11:22:33", display_name="My Tile tracker")
        self.assertEqual(device_type, DeviceType.TRACKER)

    def test_no_match_returns_unknown_with_zero_confidence(self) -> None:
        device_type, confidence = guess_device_type(mac_address="00:11:22:33:44:55", display_name="Some Random Device")
        self.assertEqual(device_type, DeviceType.UNKNOWN)
        self.assertEqual(confidence, 0.0)
