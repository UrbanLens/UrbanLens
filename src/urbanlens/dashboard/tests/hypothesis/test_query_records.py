"""Pin the *shape* of what an endpoint asks the database, not just how much.

Complementary to ``test_query_scaling.py`` rather than a replacement for it, and
the split is worth understanding before adding to either.

The scaling harness measures a *slope*: render the same endpoint at two data
sizes and fail if the query count grows with the rows. That is the right test
for a list, and it is deliberately blind to the intercept, so an endpoint that
has always cost thirty queries keeps passing.

This measures a *fingerprint*: the exact sequence of statements, normalised,
written to a ``.perf.yml`` beside this file. It catches what a slope cannot -
a detail view that gains one query per related object it renders (three rows
today, so no visible slope), a ``select_related`` dropped in a refactor, a
cache read that quietly became a database read. The cost is that any legitimate
change to a query pattern shows up as a diff somebody has to approve.

**Reading a failure.** django-perf-rec prints the statements that differ. An
added ``SELECT`` repeated per row is an N+1; a single added statement is usually
a new field or a new permission check, and the fix is to re-record with
``PERF_REC={"MODE": "overwrite"}`` after confirming it is intended.

Records are per test *method*, so renaming a test orphans its record. Delete the
stale entry when you do that; a leftover is harmless but misleading.
"""

from __future__ import annotations

from django.contrib.auth.models import User
from django.urls import reverse
import django_perf_rec
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.account.model import ApiKeyScope
from urbanlens.dashboard.models.images.model import Image
from urbanlens.dashboard.models.labels.model import Label
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.services.auth.api_keys import generate_api_key

#: Related rows attached to the pin under test. Enough that a per-row query is
#: unmistakable in the recorded fingerprint, few enough to stay readable.
_RELATED_ROWS = 3


class ExternalApiQueryRecordTests(TestCase):
    """Query fingerprints for the external API's most-fetched endpoints.

    These are the endpoints a mobile client calls on every screen, where an
    extra query per related object is paid on every request by every user.
    """

    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # absorbs the bootstrap site-admin promotion
        self.user = baker.make(User)
        self.profile = self.user.profile
        _key, self.raw_key = generate_api_key(self.user, "perf-rec")
        api_key = self.user.api_keys.first()
        api_key.scopes = list(ApiKeyScope.values)
        api_key.save()

        self.location = baker.make(Location)
        self.pin = baker.make(Pin, profile=self.profile, location=self.location)
        for _ in range(_RELATED_ROWS):
            self.pin.labels.add(baker.make(Label, profile=self.profile, kind="tag"))
            baker.make(Image, pin=self.pin, profile=self.profile, location=self.location)

    @property
    def _auth(self) -> dict[str, str]:
        return {"HTTP_AUTHORIZATION": f"Bearer {self.raw_key}"}

    def test_pin_detail_query_fingerprint(self) -> None:
        """A pin with labels and photos must not query per related row."""
        with django_perf_rec.record():
            response = self.client.get(reverse("external_api:pins.detail", args=[self.pin.slug]), **self._auth)
        self.assertEqual(response.status_code, 200)

    def test_pin_list_query_fingerprint(self) -> None:
        with django_perf_rec.record():
            response = self.client.get(reverse("external_api:pins"), **self._auth)
        self.assertEqual(response.status_code, 200)

    def test_whoami_query_fingerprint(self) -> None:
        """The cheapest authenticated call there is - a useful floor.

        If this record grows, the cost was added to *authentication* or to the
        middleware chain, which means every other endpoint grew by the same
        amount without any of their records explaining why.
        """
        with django_perf_rec.record():
            response = self.client.get(reverse("external_api:whoami"), **self._auth)
        self.assertEqual(response.status_code, 200)
