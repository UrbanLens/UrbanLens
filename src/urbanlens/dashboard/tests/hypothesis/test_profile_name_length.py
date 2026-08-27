"""An over-long name must not reach the database as a 500.

`EditProfileView._save_profile` assigns `first_name`/`last_name` directly from
`request.POST` *after* the form has saved, so the form's validation never sees them.
Both columns are `max_length=150`, and `save()` on a longer value raises DataError.

Found by the filter derived from the coverage work: a never-executed handler that
assigns model fields directly from request data instead of delegating to something
that owns the constraint.
"""

from __future__ import annotations

from django.contrib.auth.models import User
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.profile.model import Profile

LIMIT = User._meta.get_field("first_name").max_length


class ProfileNameLengthTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.user = baker.make(User, username="zzaudit-names")
        Profile.objects.get_or_create(user=self.user)
        self.client.force_login(self.user)

    def _save(self, first: str, last: str = "Ok") -> object:
        return self.client.post(reverse("profile.edit"), data={"first_name": first, "last_name": last})

    def test_an_over_long_first_name_does_not_500(self) -> None:
        response = self._save("z" * (LIMIT + 50))

        self.assertLess(response.status_code, 500)
        self.user.refresh_from_db()
        self.assertLessEqual(len(self.user.first_name), LIMIT)

    def test_an_over_long_last_name_does_not_500(self) -> None:
        response = self._save("Ok", "z" * (LIMIT + 50))

        self.assertLess(response.status_code, 500)
        self.user.refresh_from_db()
        self.assertLessEqual(len(self.user.last_name), LIMIT)

    def test_an_ordinary_name_is_stored_unchanged(self) -> None:
        """The control: truncation must not be mangling normal input."""
        self._save("Ada", "Lovelace")

        self.user.refresh_from_db()
        self.assertEqual((self.user.first_name, self.user.last_name), ("Ada", "Lovelace"))
