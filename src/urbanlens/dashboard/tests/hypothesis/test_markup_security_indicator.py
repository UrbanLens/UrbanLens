"""Tests for applying markup security indicators to pin security fields."""

from __future__ import annotations

from django.contrib.auth.models import User
from urbanlens.core.tests.testcase import TestCase
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from model_bakery import baker

from urbanlens.dashboard.controllers.markup import _apply_security_indicator
from urbanlens.dashboard.models.abstract.choices import SecurityLevel
from urbanlens.dashboard.models.pin.model import Pin

_INDICATOR_FIELD_CASES = [
	("fence", "fences"),
	("camera", "cameras"),
	("alarm", "alarms"),
	("security", "security"),
	("sign", "signs"),
	("plywood", "plywood"),
	("locked", "locked"),
	("vps", "vps"),
]

_db_settings = settings(
	max_examples=20,
	deadline=None,
	suppress_health_check=[HealthCheck.too_slow],
)


class ApplySecurityIndicatorTests(TestCase):
	"""Security markup upgrades the matching pin field without downgrading known values."""

	def _pin(self, **extra) -> Pin:
		profile = baker.make(User).profile
		return baker.make(Pin, profile=profile, **extra)

	def test_camera_indicator_upgrades_unknown_cameras_to_some(self) -> None:
		pin = self._pin(cameras=SecurityLevel.UNKNOWN)

		_apply_security_indicator(pin, "camera")

		pin.refresh_from_db()
		self.assertEqual(pin.cameras, SecurityLevel.SOME)

	def test_fence_indicator_upgrades_no_fences_to_some(self) -> None:
		pin = self._pin(fences=SecurityLevel.NO)

		_apply_security_indicator(pin, "fence")

		pin.refresh_from_db()
		self.assertEqual(pin.fences, SecurityLevel.SOME)

	def test_existing_everywhere_value_is_not_downgraded(self) -> None:
		pin = self._pin(fences=SecurityLevel.EVERYWHERE)

		_apply_security_indicator(pin, "fence")

		pin.refresh_from_db()
		self.assertEqual(pin.fences, SecurityLevel.EVERYWHERE)

	def test_unknown_indicator_does_not_change_security_fields(self) -> None:
		pin = self._pin(cameras=SecurityLevel.UNKNOWN, fences=SecurityLevel.NO, alarms=SecurityLevel.EVERYWHERE)

		_apply_security_indicator(pin, "not-a-real-indicator")

		pin.refresh_from_db()
		self.assertEqual(pin.cameras, SecurityLevel.UNKNOWN)
		self.assertEqual(pin.fences, SecurityLevel.NO)
		self.assertEqual(pin.alarms, SecurityLevel.EVERYWHERE)

	@given(st.sampled_from(_INDICATOR_FIELD_CASES))
	@_db_settings
	def test_each_indicator_upgrades_only_its_matching_field(self, indicator_field: tuple[str, str]) -> None:
		indicator, field = indicator_field
		pin = self._pin(
			fences=SecurityLevel.UNKNOWN,
			cameras=SecurityLevel.UNKNOWN,
			alarms=SecurityLevel.UNKNOWN,
			security=SecurityLevel.UNKNOWN,
			signs=SecurityLevel.UNKNOWN,
			plywood=SecurityLevel.UNKNOWN,
			locked=SecurityLevel.UNKNOWN,
			vps=SecurityLevel.UNKNOWN,
		)

		_apply_security_indicator(pin, indicator)

		pin.refresh_from_db()
		for _indicator, candidate_field in _INDICATOR_FIELD_CASES:
			with self.subTest(indicator=indicator, field=candidate_field):
				expected = SecurityLevel.SOME if candidate_field == field else SecurityLevel.UNKNOWN
				self.assertEqual(getattr(pin, candidate_field), expected)
