"""Converting a global label must never hand one profile a label that is sitting on another profile's pins.

``_apply_kind_conversion`` sets ``label.profile`` to the editor when a label becomes a category or a status. Applied
to a *global* tag - which anyone holding ``dashboard.edit_global_label`` may edit - that label keeps every
attachment it already has, so afterwards other people's pins carry a label owned by the editor: private to them,
invisible to its own bearer, and no longer what ``Label.objects.visible_to()`` says a pin can hold.
"""

from __future__ import annotations

from django.contrib.auth.models import Permission, User
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.labels.model import Label
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin

KIND_TAG = "tag"
KIND_CATEGORY = "category"


class GlobalLabelConversionTests(TestCase):
    """A global tag's kind may change; its owner may not become someone who does not own its pins."""

    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # the first user is auto-promoted to site admin
        self.editor = baker.make(User)
        self.editor.user_permissions.add(Permission.objects.get(codename="edit_global_label"))
        self.editor = User.objects.get(pk=self.editor.pk)  # permissions are cached on the instance
        self.neighbour = baker.make(User)
        self.shared_label = baker.make(Label, name="Shared Tag", kind=KIND_TAG, profile=None)
        self.neighbour_pin = baker.make(Pin, profile=self.neighbour.profile, location=baker.make(Location))
        self.neighbour_pin.labels.add(self.shared_label)
        self.client.force_login(self.editor)

    def assertNoPinCarriesAnotherProfilesLabel(self) -> None:
        for pin in Pin.objects.all().prefetch_related("labels"):
            for label in pin.labels.all():
                self.assertIn(
                    label.profile_id,
                    (None, pin.profile_id),
                    f"pin {pin.pk} (profile {pin.profile_id}) carries label {label.pk} owned by {label.profile_id}",
                )

    def test_a_global_tag_on_someone_elses_pin_cannot_become_the_editors_category(self) -> None:
        response = self.client.post(
            reverse("label.edit", kwargs={"label_kind": "tags", "label_id": self.shared_label.pk}),
            {"name": "Shared Tag", "kind": KIND_CATEGORY},
        )
        self.assertEqual(response.status_code, 400)
        self.shared_label.refresh_from_db()
        self.assertIsNone(self.shared_label.profile_id)
        self.assertEqual(self.shared_label.kind, KIND_TAG)
        self.assertNoPinCarriesAnotherProfilesLabel()

    def test_the_editors_own_tag_still_converts(self) -> None:
        own = baker.make(Label, name="Mine", kind=KIND_TAG, profile=self.editor.profile)
        response = self.client.post(
            reverse("label.edit", kwargs={"label_kind": "tags", "label_id": own.pk}),
            {"name": "Mine", "kind": KIND_CATEGORY},
        )
        self.assertLess(response.status_code, 400, response.content[:200])
        own.refresh_from_db()
        self.assertEqual(own.kind, KIND_CATEGORY)
        self.assertEqual(own.profile_id, self.editor.profile.pk)
        self.assertNoPinCarriesAnotherProfilesLabel()
