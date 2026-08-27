"""An undecryptable SiteSettings field must not take the whole site down.

``EncryptedTextField``'s docstring splits its fields in two: credentials fail
loud, because "their callers already catch ``InvalidToken`` and drop the row so
the user simply reconnects", while user-authored content sets ``fail_soft=True``
because ``Profile`` "loads on nearly every authenticated request, so one bad row
would take the whole site down for that user rather than degrading one field".

``SiteSettings.notify_gotify_token`` sat in neither camp: shaped like a
credential, but with no caller anywhere that catches ``InvalidToken`` and drops
it - and ``SiteSettings`` is a *singleton* three context processors load on every
render, for every user including anonymous ones. So a key change without
``rotate_field_encryption`` (exactly what that command exists to prevent) made
every page 500 **and** stopped the styled 500 page itself from rendering, since
it runs the same context processors.

Degrading to the field's own default is strictly better here: the token is
unusable either way, so Gotify pushes stop regardless - the only question is
whether the site stays up while an admin re-enters it. The failure is still
recorded loudly (``EncryptedTextField`` logs the model, field, and the setting to
check).
"""

from __future__ import annotations

from django.contrib.auth.models import User
from django.db import connection
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.site_settings import request_cache
from urbanlens.dashboard.models.site_settings.model import SiteSettings

#: Well-formed Fernet-looking ciphertext that no configured key can open - the
#: state a stale key leaves in the column.
_UNDECRYPTABLE = "gAAAAABlLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLL"


class SiteSettingsEncryptedDegradationTests(TestCase):
    """A singleton loaded on every request must degrade, not raise."""

    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # absorbs the bootstrap site-admin promotion
        self.user = baker.make(User)
        site = SiteSettings.get_current()
        with connection.cursor() as cursor:
            cursor.execute(
                "UPDATE dashboard_site_settings SET notify_gotify_token = %s WHERE id = %s",
                [_UNDECRYPTABLE, site.pk],
            )
        request_cache.invalidate()

    def test_the_row_still_loads(self) -> None:
        site = SiteSettings.objects.get(pk=SiteSettings.objects.values_list("pk", flat=True).first())

        self.assertEqual(site.notify_gotify_token, "")

    def test_pages_still_render(self) -> None:
        self.client.force_login(self.user)

        for name in ("home.view", "map.view"):
            with self.subTest(page=name):
                self.assertEqual(self.client.get(reverse(name)).status_code, 200)

    def test_an_anonymous_visitor_still_gets_a_page(self) -> None:
        """Context processors run for logged-out visitors too, so they were hit as well."""
        response = self.client.get(reverse("home.view"))

        self.assertLess(response.status_code, 500)
