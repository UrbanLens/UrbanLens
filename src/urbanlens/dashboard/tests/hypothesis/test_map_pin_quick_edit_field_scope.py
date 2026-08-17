"""The map's quick-edit must write only the pin fields it was given.

``MapController.patch_pin`` applies whichever of name/coordinates/icon/color/
custom_icon were posted and then bare-saves the pin - every column, from the
instance loaded at the start of the request.

``Pin`` is the most heavily written model in the app: around forty writers scope
their updates to the columns they own, and several of them are background work
that can land at any moment - visit logging setting ``last_visited``, the
placeholder-name sweep clearing ``name``, pin suggestions, and
``share_provenance`` setting ``inferred_source_share``. That last one is part of
the ``LocationExposure`` provenance chain the project treats as an invariant, so
silently reverting it is worse than losing a preference.

The window is a single request, but this request is not a short one: it accepts
a ``custom_icon`` upload (validated before the save) and can repoint the pin to
a find-or-created ``Location``.

The concurrent write is injected through a real seam - ``get_nearby_or_create``,
which patch_pin calls between loading the pin and saving it - so the interleaving
is exactly the one that happens in production, and deterministic.
"""

from __future__ import annotations

import datetime
from unittest import mock

from django.contrib.auth.models import User
from django.utils import timezone
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin


class PatchPinFieldScopeTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.user: User = baker.make(User)
        self.profile = self.user.profile
        self.client.force_login(self.user)
        self.location = baker.make(Location, latitude=41.0, longitude=-73.0)
        self.pin = baker.make(Pin, profile=self.profile, location=self.location, parent_pin=None, name="Old Mill")
        self.visited = timezone.now() - datetime.timedelta(days=1)

    def _quick_edit(self, **fields) -> None:
        """POST the quick-edit form, with a concurrent write landing mid-request.

        The write is triggered from ``get_nearby_or_create``, which patch_pin
        calls after loading the pin and before saving it.
        """
        original = Location.objects.get_nearby_or_create

        def racing(*args, **kwargs):
            Pin.objects.filter(pk=self.pin.pk).update(last_visited=self.visited)
            return original(*args, **kwargs)

        with mock.patch.object(Location.objects, "get_nearby_or_create", side_effect=racing):
            response = self.client.post(f"/dashboard/map/quick-edit/{self.pin.slug or self.pin.uuid}/", data=fields)
        self.assertEqual(response.status_code, 200, response.content)

    def test_a_quick_edit_does_not_revert_a_concurrent_write(self) -> None:
        """Visit logging sets last_visited; a quick edit must not put it back."""
        self._quick_edit(name="New Mill", latitude="41.5", longitude="-73.5")

        self.pin.refresh_from_db()
        self.assertEqual(self.pin.last_visited, self.visited, "a quick edit reverted a field another writer had set")

    def test_a_quick_edit_still_applies_what_it_was_given(self) -> None:
        """The complement: narrowing the write must not narrow it to nothing."""
        self._quick_edit(name="New Mill", latitude="41.5", longitude="-73.5", color="#ff0000")

        self.pin.refresh_from_db()
        self.assertEqual(self.pin.name, "New Mill")
        self.assertTrue(self.pin.name_is_user_provided)
        self.assertEqual(self.pin.color, "#ff0000")
        self.assertNotEqual(self.pin.location_id, self.location.pk, "moving the pin should have repointed its location")

    def test_an_omitted_field_is_left_alone(self) -> None:
        """Only what was posted is written - an absent field is not an instruction to clear."""
        Pin.objects.filter(pk=self.pin.pk).update(color="#00ff00")

        self._quick_edit(name="New Mill", latitude="41.5", longitude="-73.5")

        self.pin.refresh_from_db()
        self.assertEqual(self.pin.color, "#00ff00", "a field the request never mentioned was overwritten")
