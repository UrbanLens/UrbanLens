"""A name taken from a request must not reach a bounded column unchecked.

``test_profile_name_length`` found one instance of this - ``EditProfileView``
assigning ``first_name``/``last_name`` straight from ``request.POST`` past the
form that owned the constraint - and fixed that view. It is a class, not an
incident: a scan for *request data reaching a bounded CharField with no length
check in between* finds the same shape in several controllers.

The codebase already owns the answer. ``services/core/text_limits.text_length_error``
returns a message and callers turn it into a 400, and the description fields
next to these very names use it. The names were simply missed - ``PinList``
checks its description's length and not its own name's, in the same function.

Truncating is wrong here, unlike ``NotificationLog.title``: these are names the
user chose and will look for again, so a 400 that says what the limit is beats
silently storing something they did not type.

Each test drives a real endpoint, because the question is whether a *request*
can produce the 500, not whether the model rejects long strings.
"""

from __future__ import annotations

from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.aliases.model import PinAlias
from urbanlens.dashboard.models.labels.model import Label
from urbanlens.dashboard.models.pin_list.model import PinList
from urbanlens.dashboard.models.saved_filter.model import SavedFilter


def _too_long(model, field: str) -> str:
    return "n" * (model._meta.get_field(field).max_length + 1)


class RequestNameLengthTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.user = baker.make("auth.User")
        self.profile = self.user.profile
        self.client.force_login(self.user)

    def test_label_create_rejects_an_overlong_name(self) -> None:
        response = self.client.post(reverse("label.create", kwargs={"label_kind": "tags"}), {"name": _too_long(Label, "name")})

        self.assertEqual(response.status_code, 400)
        # Not `Label.objects.exists()` - the app seeds global labels, so only
        # the name under test says anything about this request.
        self.assertFalse(Label.objects.filter(name__startswith="nnn").exists())

    def test_list_create_rejects_an_overlong_name(self) -> None:
        response = self.client.post(reverse("lists.create"), {"name": _too_long(PinList, "name")})

        self.assertEqual(response.status_code, 400)
        self.assertFalse(PinList.objects.exists())

    def test_saved_filter_create_rejects_an_overlong_name(self) -> None:
        response = self.client.post(reverse("saved_filters.create"), {"filter_name": _too_long(SavedFilter, "name"), "search": "x"})

        self.assertNotEqual(response.status_code, 500)
        self.assertFalse(SavedFilter.objects.filter(name__startswith="nnn").exists())

    def test_pin_alias_create_rejects_an_overlong_name(self) -> None:
        """`sanitize_name` drops characters but does not bound length."""
        pin = baker.make("dashboard.Pin", profile=self.profile, location=baker.make("dashboard.Location"))

        response = self.client.post(reverse("pin.aliases", kwargs={"pin_slug": pin.slug}), {"name": _too_long(PinAlias, "name")})

        self.assertEqual(response.status_code, 400)
        self.assertFalse(PinAlias.objects.filter(name__startswith="nnn").exists())

    def test_a_name_at_exactly_the_limit_is_still_accepted(self) -> None:
        """The boundary belongs to the user, not to the error path."""
        limit = PinList._meta.get_field("name").max_length

        response = self.client.post(reverse("lists.create"), {"name": "n" * limit})

        self.assertNotEqual(response.status_code, 400)
        self.assertTrue(PinList.objects.filter(name="n" * limit).exists())
