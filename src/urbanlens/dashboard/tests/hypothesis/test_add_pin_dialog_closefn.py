"""Regression coverage for UL-32 on the "Add Pin" dialog specifically."""

from __future__ import annotations

from pathlib import Path

from django.contrib.auth.models import User
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase

_SCRIPT_SOURCE = (Path(__file__).resolve().parents[3] / "dashboard/frontend/ts/entries/map-page.ts").read_text()


class AddPinDialogUsesSharedCloseHandlerTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.user = baker.make(User)
        self.client.force_login(self.user)

    def test_dialog_declares_its_close_function_for_the_shared_handler(self) -> None:
        body = self.client.get(reverse("map.view")).content.decode()
        self.assertIn('id="add-pin-dialog"', body)
        self.assertIn('data-closefn="closeAddPinDialog"', body)

    def test_the_duplicate_per_dialog_drag_guard_is_gone(self) -> None:
        """Regression guard against re-introducing the removed duplicate -
        the variable names below were unique to that dead copy."""
        body = self.client.get(reverse("map.view")).content.decode()
        self.assertNotIn("apIsBackdrop", body)
        self.assertNotIn("var apDlg", body)

    def test_close_add_pin_dialog_still_does_its_cleanup(self) -> None:
        """closeAddPinDialog() must still exist with its real cleanup logic - the shared handler calls it by name (window['closeAddPinDialog']()) rather than a bare dialog.close(), specifically so this cleanup still runs on a backdrop click, not just on explicit Cancel/Escape."""
        fn_index = _SCRIPT_SOURCE.find("function closeAddPinDialog()")
        self.assertNotEqual(fn_index, -1)
        fn_body = _SCRIPT_SOURCE[fn_index : _SCRIPT_SOURCE.find("\n}", fn_index)]
        self.assertIn("_addPinClickMode = false", fn_body)
        self.assertIn("map.removeLayer(_addPinMarker)", fn_body)
        self.assertIn("window.closeAddPinDialog = closeAddPinDialog", _SCRIPT_SOURCE)
