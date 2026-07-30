"""Tests for services.device_scan.type_guessing.

The core invariant: a client-supplied guess always wins and is never
overwritten by a heuristic re-guess on a later upload with no guess of its
own - see resolve_device_type's docstring.
"""

from __future__ import annotations

from django.test import SimpleTestCase

from urbanlens.dashboard.models.device_scan.model import DeviceType, DeviceTypeSource
from urbanlens.dashboard.services.device_scan.type_guessing import guess_device_type, resolve_device_type


class GuessDeviceTypeTests(SimpleTestCase):
    """Pure name/OUI heuristic, independent of resolve_device_type's gating."""

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


class ResolveDeviceTypeTests(SimpleTestCase):
    """The gating rules around client vs. heuristic classification."""

    def test_client_guess_always_wins(self) -> None:
        device_type, source = resolve_device_type(
            current_type=DeviceType.UNKNOWN,
            current_source=DeviceTypeSource.UNSET,
            client_guess=DeviceType.SENSOR,
            mac_address="00:00:00:00:00:00",
            display_name="",
        )
        self.assertEqual(device_type, DeviceType.SENSOR)
        self.assertEqual(source, DeviceTypeSource.CLIENT)

    def test_client_guess_overrides_prior_heuristic_classification(self) -> None:
        device_type, source = resolve_device_type(
            current_type=DeviceType.CAMERA,
            current_source=DeviceTypeSource.HEURISTIC,
            client_guess=DeviceType.OTHER,
            mac_address="00:00:00:00:00:00",
            display_name="",
        )
        self.assertEqual(device_type, DeviceType.OTHER)
        self.assertEqual(source, DeviceTypeSource.CLIENT)

    def test_heuristic_runs_only_when_currently_unset_and_no_client_guess(self) -> None:
        device_type, source = resolve_device_type(
            current_type=DeviceType.UNKNOWN,
            current_source=DeviceTypeSource.UNSET,
            client_guess=None,
            mac_address="8C:C8:F4:11:22:33",
            display_name="",
        )
        self.assertEqual(device_type, DeviceType.CAMERA)
        self.assertEqual(source, DeviceTypeSource.HEURISTIC)

    def test_heuristic_does_not_run_once_already_classified(self) -> None:
        """No flip-flopping: a device already classified stays as-is absent a client guess."""
        device_type, source = resolve_device_type(
            current_type=DeviceType.CAMERA,
            current_source=DeviceTypeSource.HEURISTIC,
            client_guess=None,
            mac_address="18:B4:30:11:22:33",  # a SENSOR-mapped OUI
            display_name="",
        )
        self.assertEqual(device_type, DeviceType.CAMERA)
        self.assertEqual(source, DeviceTypeSource.HEURISTIC)

    def test_heuristic_does_not_run_once_client_classified(self) -> None:
        device_type, source = resolve_device_type(
            current_type=DeviceType.OTHER,
            current_source=DeviceTypeSource.CLIENT,
            client_guess=None,
            mac_address="8C:C8:F4:11:22:33",
            display_name="",
        )
        self.assertEqual(device_type, DeviceType.OTHER)
        self.assertEqual(source, DeviceTypeSource.CLIENT)

    def test_no_heuristic_match_leaves_unset_device_unknown(self) -> None:
        device_type, source = resolve_device_type(
            current_type=DeviceType.UNKNOWN,
            current_source=DeviceTypeSource.UNSET,
            client_guess=None,
            mac_address="00:11:22:33:44:55",
            display_name="",
        )
        self.assertEqual(device_type, DeviceType.UNKNOWN)
        self.assertEqual(source, DeviceTypeSource.UNSET)

    def test_empty_string_client_guess_is_treated_as_no_guess(self) -> None:
        """The view passes ``entry.device_type_guess or None`` - an empty string must behave identically to None."""
        device_type, source = resolve_device_type(
            current_type=DeviceType.UNKNOWN,
            current_source=DeviceTypeSource.UNSET,
            client_guess="",
            mac_address="8C:C8:F4:11:22:33",
            display_name="",
        )
        self.assertEqual(device_type, DeviceType.CAMERA)
        self.assertEqual(source, DeviceTypeSource.HEURISTIC)
