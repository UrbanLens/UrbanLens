"""Regression coverage for UL-32 on the "Add Pin" dialog specifically.

UL-32 ("clicking in a dialog and dragging outside unexpectedly closes it")
was already fixed site-wide by a document-level mousedown/click handler in
themes/base.html that only closes a <dialog> when *both* the mousedown and
the click land on the backdrop - a drag that starts inside is tracked as
'inside' and never closes. The add-pin dialog additionally had its own,
independent copy of that exact same drag-guard algorithm, wired to call
closeAddPinDialog() (which does real cleanup: clears the in-progress pin
marker and click-placement mode, not just a bare dialog.close()).

That per-dialog copy was pure duplication - the site-wide handler already
supports routing to a custom close function via a `data-closefn` attribute,
just no dialog was using it yet. This asserts the dialog is wired into the
shared mechanism instead of carrying its own copy of the algorithm.
"""

from __future__ import annotations

from django.contrib.auth.models import User
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase


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
        """closeAddPinDialog() must still exist with its real cleanup logic -
        the shared handler calls it by name (window['closeAddPinDialog']())
        rather than a bare dialog.close(), specifically so this cleanup still
        runs on a backdrop click, not just on explicit Cancel/Escape."""
        body = self.client.get(reverse("map.view")).content.decode()
        fn_index = body.find("function closeAddPinDialog()")
        self.assertNotEqual(fn_index, -1)
        fn_body_end = body.find("\n    }", fn_index)
        fn_body = body[fn_index:fn_body_end]
        self.assertIn("_addPinClickMode = false", fn_body)
        self.assertIn("map.removeLayer(_addPinMarker)", fn_body)
