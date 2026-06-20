"""Tests for MapCenterForm, StyleSettingsForm, and MapDisplayForm.

Invariants verified:
  - MapCenterForm.clean_map_default_zoom returns 13 when the field is omitted,
    and passes through any valid value in [1, 19] unchanged.
  - MapDisplayForm.use_pin_cache is optional: omitting it (unchecked) is valid
    and produces False; submitting it produces True.
  - MapDisplayForm saves use_pin_cache correctly to the Profile.
"""
from __future__ import annotations

from hypothesis import HealthCheck, given, settings
from hypothesis.extra.django import TestCase as HypothesisTestCase
from model_bakery import baker

from urbanlens.dashboard.forms.settings_form import MapCenterForm, MapDisplayForm, StyleSettingsForm
from urbanlens.dashboard.models.profile.model import MapCenterMode, Profile
from urbanlens.dashboard.tests.hypothesis.strategies import valid_zoom

_db_settings = settings(
	max_examples=30,
	deadline=None,
	suppress_health_check=[HealthCheck.too_slow, HealthCheck.filter_too_much],
)


def _profile() -> Profile:
	return baker.make("auth.User").profile


# ── MapCenterForm.clean_map_default_zoom ──────────────────────────────────────

class MapCenterFormZoomCleanTests(HypothesisTestCase):
	"""clean_map_default_zoom must default to 13 when the field is blank."""

	def _submit(self, zoom_value: str, mode: str = MapCenterMode.AUTO) -> MapCenterForm:
		profile = _profile()
		form = MapCenterForm(
			data={"map_center_mode": mode, "map_default_zoom": zoom_value},
			instance=profile,
		)
		form.is_valid()
		return form

	def test_blank_zoom_defaults_to_13(self) -> None:
		form = self._submit("")
		self.assertEqual(form.cleaned_data["map_default_zoom"], 13)

	def test_explicit_zoom_1_passes_through(self) -> None:
		form = self._submit("1")
		self.assertEqual(form.cleaned_data["map_default_zoom"], 1)

	def test_explicit_zoom_19_passes_through(self) -> None:
		form = self._submit("19")
		self.assertEqual(form.cleaned_data["map_default_zoom"], 19)

	def test_explicit_zoom_13_passes_through(self) -> None:
		form = self._submit("13")
		self.assertEqual(form.cleaned_data["map_default_zoom"], 13)

	@given(zoom=valid_zoom)
	@_db_settings
	def test_any_valid_zoom_passes_through_unchanged(self, zoom: int) -> None:
		form = self._submit(str(zoom))
		self.assertTrue(form.is_valid(), form.errors)
		self.assertEqual(form.cleaned_data["map_default_zoom"], zoom)

	def test_form_is_invalid_for_zoom_below_1(self) -> None:
		form = self._submit("0")
		self.assertFalse(form.is_valid())
		self.assertIn("map_default_zoom", form.errors)

	def test_form_is_invalid_for_zoom_above_19(self) -> None:
		form = self._submit("20")
		self.assertFalse(form.is_valid())
		self.assertIn("map_default_zoom", form.errors)


# ── MapCenterForm — mode choices ──────────────────────────────────────────────

class MapCenterFormModeTests(HypothesisTestCase):
	"""All three MapCenterMode values must be accepted by the form."""

	def test_auto_mode_is_valid(self) -> None:
		profile = _profile()
		form = MapCenterForm(data={"map_center_mode": MapCenterMode.AUTO}, instance=profile)
		self.assertTrue(form.is_valid(), form.errors)

	def test_gps_mode_is_valid(self) -> None:
		profile = _profile()
		form = MapCenterForm(data={"map_center_mode": MapCenterMode.GPS}, instance=profile)
		self.assertTrue(form.is_valid(), form.errors)

	def test_custom_mode_is_valid(self) -> None:
		profile = _profile()
		form = MapCenterForm(
			data={
				"map_center_mode": MapCenterMode.CUSTOM,
				"map_custom_latitude": "42.65",
				"map_custom_longitude": "-73.75",
			},
			instance=profile,
		)
		self.assertTrue(form.is_valid(), form.errors)

	def test_invalid_mode_is_rejected(self) -> None:
		profile = _profile()
		form = MapCenterForm(data={"map_center_mode": "invalid_mode"}, instance=profile)
		self.assertFalse(form.is_valid())
		self.assertIn("map_center_mode", form.errors)


# ── MapDisplayForm.use_pin_cache ──────────────────────────────────────────────

class MapDisplayFormUsePinCacheTests(HypothesisTestCase):
	"""use_pin_cache is optional; omitting it (unchecked) must be valid and produce False."""

	def _map_data(self, **extra) -> dict:
		return {"default_map_view": "satellite", **extra}

	def test_omitting_use_pin_cache_is_valid(self) -> None:
		profile = _profile()
		form = MapDisplayForm(data=self._map_data(), instance=profile)
		self.assertTrue(form.is_valid(), form.errors)

	def test_omitting_use_pin_cache_produces_false(self) -> None:
		profile = _profile()
		form = MapDisplayForm(data=self._map_data(), instance=profile)
		form.is_valid()
		self.assertFalse(form.cleaned_data["use_pin_cache"])

	def test_submitting_use_pin_cache_produces_true(self) -> None:
		profile = _profile()
		form = MapDisplayForm(data=self._map_data(use_pin_cache="on"), instance=profile)
		form.is_valid()
		self.assertTrue(form.cleaned_data["use_pin_cache"])

	def test_saving_use_pin_cache_true_persists_to_db(self) -> None:
		profile = _profile()
		form = MapDisplayForm(data=self._map_data(use_pin_cache="on"), instance=profile)
		self.assertTrue(form.is_valid(), form.errors)
		form.save()
		profile.refresh_from_db()
		self.assertTrue(profile.use_pin_cache)

	def test_saving_use_pin_cache_false_persists_to_db(self) -> None:
		profile = _profile()
		# Ensure it starts as True so the change is meaningful.
		Profile.objects.filter(pk=profile.pk).update(use_pin_cache=True)
		form = MapDisplayForm(data=self._map_data(), instance=profile)
		self.assertTrue(form.is_valid(), form.errors)
		form.save()
		profile.refresh_from_db()
		self.assertFalse(profile.use_pin_cache)

	def test_new_profile_has_use_pin_cache_true_by_default(self) -> None:
		profile = _profile()
		self.assertTrue(profile.use_pin_cache)


# ── MapCenterForm.save() ──────────────────────────────────────────────────────

class MapCenterFormSaveTests(HypothesisTestCase):
	"""MapCenterForm.save() preserves DB custom coordinates when mode is not CUSTOM."""

	def _submit(self, profile, mode: str, lat: str = "", lng: str = "") -> MapCenterForm:
		data = {"map_center_mode": mode}
		if lat:
			data["map_custom_latitude"] = lat
		if lng:
			data["map_custom_longitude"] = lng
		form = MapCenterForm(data=data, instance=profile)
		self.assertTrue(form.is_valid(), form.errors)
		return form

	def test_custom_mode_saves_form_coordinates(self) -> None:
		profile = _profile()
		form = self._submit(profile, MapCenterMode.CUSTOM, lat="42.123456", lng="-73.654321")
		form.save()
		profile.refresh_from_db()
		self.assertAlmostEqual(float(profile.map_custom_latitude), 42.123456, places=4)
		self.assertAlmostEqual(float(profile.map_custom_longitude), -73.654321, places=4)

	def test_auto_mode_preserves_db_custom_coordinates(self) -> None:
		from decimal import Decimal
		profile = _profile()
		# Pre-set custom coords in the DB.
		from urbanlens.dashboard.models.profile.model import Profile
		Profile.objects.filter(pk=profile.pk).update(
			map_custom_latitude=Decimal("40.000000"),
			map_custom_longitude=Decimal("-75.000000"),
		)
		# Submit with AUTO mode and different coords in the hidden fields.
		form = self._submit(profile, MapCenterMode.AUTO, lat="99.000000", lng="99.000000")
		form.save()
		profile.refresh_from_db()
		# The form's fake coords must NOT have overwritten the stored ones.
		self.assertAlmostEqual(float(profile.map_custom_latitude), 40.0, places=1)
		self.assertAlmostEqual(float(profile.map_custom_longitude), -75.0, places=1)

	def test_gps_mode_preserves_db_custom_coordinates(self) -> None:
		from decimal import Decimal
		profile = _profile()
		from urbanlens.dashboard.models.profile.model import Profile
		Profile.objects.filter(pk=profile.pk).update(
			map_custom_latitude=Decimal("51.500000"),
			map_custom_longitude=Decimal("-0.120000"),
		)
		form = self._submit(profile, MapCenterMode.GPS, lat="0.000000", lng="0.000000")
		form.save()
		profile.refresh_from_db()
		self.assertAlmostEqual(float(profile.map_custom_latitude), 51.5, places=1)
		self.assertAlmostEqual(float(profile.map_custom_longitude), -0.12, places=1)

	def test_auto_mode_with_no_prior_coords_sets_none(self) -> None:
		profile = _profile()
		# Ensure no custom coords stored.
		from urbanlens.dashboard.models.profile.model import Profile
		Profile.objects.filter(pk=profile.pk).update(
			map_custom_latitude=None,
			map_custom_longitude=None,
		)
		form = self._submit(profile, MapCenterMode.AUTO)
		form.save()
		profile.refresh_from_db()
		self.assertIsNone(profile.map_custom_latitude)
		self.assertIsNone(profile.map_custom_longitude)

	def test_save_with_commit_false_does_not_write_to_db(self) -> None:
		profile = _profile()
		from urbanlens.dashboard.models.profile.model import Profile, MapCenterMode as MCM
		original_mode = profile.map_center_mode
		form = self._submit(profile, MapCenterMode.GPS)
		instance = form.save(commit=False)
		# Reload from DB — it should not have changed.
		db_profile = Profile.objects.get(pk=profile.pk)
		self.assertEqual(db_profile.map_center_mode, original_mode)
