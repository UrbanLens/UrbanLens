"""An upload the proxy will reject has to be refused here, where it can be explained."""

from __future__ import annotations

from django.test import override_settings

from hypothesis import given, settings as hypothesis_settings, strategies as st
from urbanlens.core.tests.testcase import SimpleTestCase, TestCase
from urbanlens.dashboard.services.media.storage import (
    cap_to_ingress,
    ingress_body_limit_bytes,
    max_upload_file_size_bytes,
)

_MB = 1_000_000


class IngressCapTests(SimpleTestCase):
    """The clamp itself."""

    def test_no_cap_configured_changes_nothing(self) -> None:
        with override_settings(MAX_REQUEST_BODY_BYTES=0):
            self.assertEqual(ingress_body_limit_bytes(), 0)
            self.assertEqual(cap_to_ingress(500 * _MB), 500 * _MB)

    def test_a_configured_cap_lowers_a_larger_limit(self) -> None:
        with override_settings(MAX_REQUEST_BODY_BYTES=100 * _MB):
            self.assertEqual(cap_to_ingress(500 * _MB), 100 * _MB)

    def test_a_configured_cap_leaves_a_smaller_limit_alone(self) -> None:
        """It is a ceiling, not a target - it must never raise a deliberate limit."""
        with override_settings(MAX_REQUEST_BODY_BYTES=100 * _MB):
            self.assertEqual(cap_to_ingress(25 * _MB), 25 * _MB)

    @given(limit=st.integers(min_value=0, max_value=2_000 * _MB), cap=st.integers(min_value=0, max_value=2_000 * _MB))
    @hypothesis_settings(max_examples=100, deadline=None)
    def test_the_result_never_exceeds_either_input(self, limit: int, cap: int) -> None:
        with override_settings(MAX_REQUEST_BODY_BYTES=cap):
            result = cap_to_ingress(limit)
        self.assertLessEqual(result, limit)
        if cap:
            self.assertLessEqual(result, cap)

    def test_the_setting_exists_in_the_real_settings_module(self) -> None:
        """An override that invents a setting proves nothing about production."""
        from django.conf import settings as django_settings

        self.assertTrue(hasattr(django_settings, "MAX_REQUEST_BODY_BYTES"))
        self.assertIsInstance(django_settings.MAX_REQUEST_BODY_BYTES, int)


class AdvertisedUploadSizeTests(TestCase):
    """The number the browser pre-checks against, and the server enforces, is the same one."""

    def setUp(self) -> None:
        super().setUp()
        from urbanlens.dashboard.models.site_settings.model import SiteSettings

        site_settings = SiteSettings.get_current()
        site_settings.max_upload_file_size_mb = 250
        site_settings.save(update_fields=["max_upload_file_size_mb"])

    def test_uncapped_deployment_advertises_the_admins_number(self) -> None:
        with override_settings(MAX_REQUEST_BODY_BYTES=0):
            self.assertEqual(max_upload_file_size_bytes(), 250 * _MB)

    def test_capped_deployment_advertises_the_ingress_number(self) -> None:
        with override_settings(MAX_REQUEST_BODY_BYTES=100 * _MB):
            self.assertEqual(max_upload_file_size_bytes(), 100 * _MB)

    def test_the_server_side_check_refuses_at_the_capped_size(self) -> None:
        from urbanlens.dashboard.services.media.storage import file_size_error_for_upload

        with override_settings(MAX_REQUEST_BODY_BYTES=100 * _MB):
            self.assertIsNone(file_size_error_for_upload(99 * _MB))
            error = file_size_error_for_upload(150 * _MB)
        self.assertIsNotNone(error)
        self.assertIn("too large", error)

    def test_the_same_size_is_accepted_when_no_cap_is_configured(self) -> None:
        """The negative half: the refusal above must come from the cap, not from the size."""
        from urbanlens.dashboard.services.media.storage import file_size_error_for_upload

        with override_settings(MAX_REQUEST_BODY_BYTES=0):
            self.assertIsNone(file_size_error_for_upload(150 * _MB))


class DataFileUploadFormCapTests(SimpleTestCase):
    """The import form has its own 500 MB literal, which the cap also has to reach."""

    def _validate(self, size: int) -> str | None:
        """Run the form field's own per-file validation against a stand-in upload.

        Args:
            size: The file's size in bytes.

        Returns:
            The validation message, or None when the file is accepted."""
        from django import forms

        from urbanlens.dashboard.forms.upload_datafile import _MultipleFileField

        class _FakeUpload:
            name = "takeout.zip"

            def __init__(self, size: int) -> None:
                self.size = size

        try:
            _MultipleFileField()._validate_upload(_FakeUpload(size))  # noqa: SLF001 - the unit under test
        except forms.ValidationError as exc:
            return str(exc)
        return None

    def test_capped_deployment_refuses_over_the_cap(self) -> None:
        with override_settings(MAX_REQUEST_BODY_BYTES=100 * _MB):
            message = self._validate(150 * _MB)
        self.assertIsNotNone(message)
        self.assertIn("too large", message)

    def test_uncapped_deployment_still_accepts_the_same_file(self) -> None:
        with override_settings(MAX_REQUEST_BODY_BYTES=0):
            self.assertIsNone(self._validate(150 * _MB))

    def test_the_form_still_refuses_past_its_own_limit_with_no_cap(self) -> None:
        with override_settings(MAX_REQUEST_BODY_BYTES=0):
            self.assertIsNotNone(self._validate(600 * _MB))
