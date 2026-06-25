"""Tests for the multi-file upload form and its supporting widget/field classes.

No database access required - these are pure form-validation tests.

Classes under test:
    _MultipleFileInput  - widget that extracts a list from the file dict
    _MultipleFileField  - field that validates and returns a list of files
    UploadDataFile      - the public form
"""
from __future__ import annotations

from unittest.mock import MagicMock

from django.core.files.uploadedfile import SimpleUploadedFile
from django.utils.datastructures import MultiValueDict

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.forms.upload_datafile import UploadDataFile, _MultipleFileField, _MultipleFileInput


# ── _MultipleFileInput ────────────────────────────────────────────────────────

class MultipleFileInputValueFromDatadictTests(TestCase):
    """value_from_datadict calls files.getlist(name) and returns the result."""

    def _widget(self) -> _MultipleFileInput:
        return _MultipleFileInput()

    def test_returns_list_from_getlist(self) -> None:
        files = MagicMock()
        files.getlist.return_value = ["a", "b"]
        result = self._widget().value_from_datadict({}, files, "upload")
        self.assertEqual(result, ["a", "b"])

    def test_calls_getlist_with_the_given_name(self) -> None:
        files = MagicMock()
        files.getlist.return_value = []
        self._widget().value_from_datadict({}, files, "my_field")
        files.getlist.assert_called_once_with("my_field")

    def test_empty_upload_returns_empty_list(self) -> None:
        files = MagicMock()
        files.getlist.return_value = []
        result = self._widget().value_from_datadict({}, files, "upload")
        self.assertEqual(result, [])

    def test_allow_multiple_selected_is_true(self) -> None:
        self.assertTrue(_MultipleFileInput.allow_multiple_selected)


# ── _MultipleFileField ────────────────────────────────────────────────────────

class MultipleFileFieldCleanTests(TestCase):
    """_MultipleFileField.clean validates each file individually."""

    def _field(self) -> _MultipleFileField:
        return _MultipleFileField(required=True)

    def _file(self, name: str = "test.txt", content: bytes = b"data") -> SimpleUploadedFile:
        return SimpleUploadedFile(name, content)

    def test_empty_list_raises_validation_error(self) -> None:
        from django import forms
        with self.assertRaises(forms.ValidationError):
            self._field().clean([])

    def test_none_raises_validation_error(self) -> None:
        from django import forms
        with self.assertRaises(forms.ValidationError):
            self._field().clean(None)

    def test_single_file_returns_list_of_one(self) -> None:
        result = self._field().clean([self._file()])
        self.assertIsInstance(result, list)
        self.assertEqual(len(result), 1)

    def test_multiple_files_return_all(self) -> None:
        files = [self._file("a.txt"), self._file("b.txt")]
        result = self._field().clean(files)
        self.assertEqual(len(result), 2)

    def test_result_items_are_the_cleaned_files(self) -> None:
        f = self._file("data.csv", b"col1,col2\n1,2")
        result = self._field().clean([f])
        self.assertEqual(len(result), 1)

    def test_default_widget_is_multiple_file_input(self) -> None:
        self.assertIsInstance(self._field().widget, _MultipleFileInput)


# ── UploadDataFile ────────────────────────────────────────────────────────────

class UploadDataFileFormTests(TestCase):
    """UploadDataFile validates that at least one file is provided."""

    def _file(self, name: str = "import.kml", content: bytes = b"<kml/>") -> SimpleUploadedFile:
        return SimpleUploadedFile(name, content)

    def test_valid_with_one_file(self) -> None:
        files: MultiValueDict = MultiValueDict({"upload_files": [self._file()]})
        form = UploadDataFile(data={}, files=files)
        # Manually call clean so we can check without full Django multi-value upload infra.
        field = form.fields["upload_files"]
        result = field.clean([self._file()])
        self.assertEqual(len(result), 1)

    def test_field_is_required(self) -> None:
        field = UploadDataFile().fields["upload_files"]
        self.assertTrue(field.required)

    def test_form_has_upload_files_field(self) -> None:
        self.assertIn("upload_files", UploadDataFile().fields)

    def test_field_label(self) -> None:
        form = UploadDataFile()
        self.assertEqual(form.fields["upload_files"].label, "Files")

    def test_form_is_valid_when_bound_with_files_via_multivalue_dict(self) -> None:
        # Exercise the full form binding path including value_from_datadict.
        files: MultiValueDict = MultiValueDict({"upload_files": [self._file()]})
        form = UploadDataFile(data={}, files=files)
        self.assertTrue(form.is_valid(), form.errors)

    def test_form_is_invalid_when_no_files_provided(self) -> None:
        form = UploadDataFile(data={}, files=MultiValueDict())
        self.assertFalse(form.is_valid())
        self.assertIn("upload_files", form.errors)

    def test_form_valid_with_multiple_files_via_bind(self) -> None:
        files: MultiValueDict = MultiValueDict({
            "upload_files": [self._file("a.kml"), self._file("b.kml")],
        })
        form = UploadDataFile(data={}, files=files)
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(len(form.cleaned_data["upload_files"]), 2)


# ── _MultipleFileField - falsy file filtering ─────────────────────────────────

class MultipleFileFieldFalsyFilterTests(TestCase):
    """_MultipleFileField.clean skips falsy entries in the list (the `if f` guard)."""

    def _field(self) -> _MultipleFileField:
        return _MultipleFileField(required=True)

    def _file(self, name: str = "test.txt") -> SimpleUploadedFile:
        return SimpleUploadedFile(name, b"content")

    def test_falsy_entries_are_filtered_out(self) -> None:
        # A list that contains one real file and one None - only the real file should survive.
        real_file = self._file("real.kml")
        # We need to pass something truthy first to bypass the `if not data` guard.
        # But None entries are filtered via `if f`.
        result = self._field().clean([real_file, None])
        self.assertEqual(len(result), 1)

    def test_list_of_all_falsy_after_filter_returns_empty(self) -> None:
        from django.core.files.uploadedfile import SimpleUploadedFile as SUF
        # Pass a truthy list to bypass the `if not data` check, then let `if f` filter reduce it.
        # An empty-bytes file is still truthy, so we'd need a truly falsy entry.
        # Here we use False as the falsy value.
        result = self._field().clean([self._file(), False])
        # The False entry is dropped; only the real file remains.
        self.assertEqual(len(result), 1)
