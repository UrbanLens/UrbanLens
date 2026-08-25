"""External API guard: the mobile/API-key surface must honor the same
subscriber gate as the web UI on officially-sourced property owner data.

``services.property.owner_access`` exists precisely so every surface that
renders owner identity/contact details asks the same question - see its
module docstring and ``test_property_owner_access.py`` for the web-UI half.
``WikiOwnershipView``/``WikiPropertySalesView`` (``external_api/views_wiki.py``)
originally queried ``WikiOwner``/``WikiPropertySale`` straight into their
serializers with no call into that module at all, so any API key with the
generic ``wiki:read`` scope - no subscription required - could pull an
official owner's name, mailing address, phone and email for free. Found by
the round-3 FEATURES.md-vs-code audit; this file is the regression guard.
"""

from __future__ import annotations

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from model_bakery import baker

from urbanlens.dashboard.models.account.model import ApiKeyScope
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.property_owner.meta import OwnerSource
from urbanlens.dashboard.models.property_owner.model import WikiOwner, WikiPropertySale
from urbanlens.dashboard.models.subscriptions.model import SiteFeature, SubscriptionRole, grant_subscription
from urbanlens.dashboard.models.wiki.model import Wiki
from urbanlens.dashboard.services.auth.api_keys import generate_api_key

_OFFICIAL_NAME = "Hudson Heritage LLC"
_OFFICIAL_ADDRESS = "1 Main St"
_OFFICIAL_PHONE = "555-0100"
_OFFICIAL_EMAIL = "records@hudsonheritage.example"
_USER_NAME = "Community Contributed Owner"


def _plain_user() -> User:
    """A user with no subscription and no feature grants.

    The first user in a fresh test database is auto-promoted to bootstrap
    site admin, which ``user_has_feature`` grants every feature - so a
    throwaway user absorbs that promotion, matching
    ``test_property_owner_access.py``'s own precedent.
    """
    baker.make(User)
    return baker.make(User)


def _subscriber() -> User:
    """A user holding an active role that grants the property-owners feature."""
    user = _plain_user()
    role = baker.make(SubscriptionRole, features=SiteFeature.PROPERTY_OWNERS)
    grant_subscription(user, role, user, None)
    return user


def _bearer(raw_key: str) -> dict:
    """Build the Authorization header kwargs for a raw API key."""
    return {"HTTP_AUTHORIZATION": f"Bearer {raw_key}"}


def _key_with_scopes(user: User) -> str:
    """Issue an API key for *user* carrying only ``WIKI_READ`` - the scope
    both endpoints under test require and nothing subscription-related."""
    api_key, raw_key = generate_api_key(user, "Test Key")
    api_key.scopes = [ApiKeyScope.WIKI_READ.value]
    api_key.save(update_fields=["scopes"])
    return raw_key


class _OwnerApiTestCase(TestCase):
    """Shared fixture: a location with one official and one user-contributed owner."""

    def setUp(self) -> None:
        super().setUp()
        self.location = baker.make(Location)
        baker.make(Wiki, location=self.location)
        self.official = baker.make(
            WikiOwner,
            name=_OFFICIAL_NAME,
            source=OwnerSource.OFFICIAL,
            address=_OFFICIAL_ADDRESS,
            phone=_OFFICIAL_PHONE,
            email=_OFFICIAL_EMAIL,
        )
        self.official.locations.add(self.location)
        self.contributed = baker.make(WikiOwner, name=_USER_NAME, source=OwnerSource.USER)
        self.contributed.locations.add(self.location)

    def _key_for(self, user: User) -> str:
        """Give *user* a pin at the fixture location (to discover the wiki) plus a scoped API key."""
        baker.make_recipe("dashboard.pin", profile=user.profile, location=self.location)
        return _key_with_scopes(user)


class WikiOwnershipApiGateTests(_OwnerApiTestCase):
    def _get(self, raw_key: str):
        return self.client.get(reverse("external_api:wikis.ownership", kwargs={"location_slug": self.location.slug}), **_bearer(raw_key))

    def test_a_plain_users_key_never_receives_the_official_owners_contact_details(self) -> None:
        response = self._get(self._key_for(_plain_user()))
        self.assertEqual(response.status_code, 200)
        body = response.json()
        names = [row["name"] for row in body["results"]]
        self.assertEqual(names, [_USER_NAME])
        raw = response.content.decode()
        self.assertNotIn(_OFFICIAL_NAME, raw)
        self.assertNotIn(_OFFICIAL_ADDRESS, raw)
        self.assertNotIn(_OFFICIAL_PHONE, raw)
        self.assertNotIn(_OFFICIAL_EMAIL, raw)

    def test_a_subscribers_key_receives_both_owners(self) -> None:
        response = self._get(self._key_for(_subscriber()))
        self.assertEqual(response.status_code, 200)
        names = {row["name"] for row in response.json()["results"]}
        self.assertEqual(names, {_OFFICIAL_NAME, _USER_NAME})

    def test_only_a_contributed_owner_yields_the_same_result_for_everyone(self) -> None:
        """No official row at all - gating must not hide user-contributed data either."""
        self.official.locations.remove(self.location)
        plain_response = self._get(self._key_for(_plain_user()))
        subscriber_response = self._get(self._key_for(_subscriber()))
        self.assertEqual([row["name"] for row in plain_response.json()["results"]], [_USER_NAME])
        self.assertEqual([row["name"] for row in subscriber_response.json()["results"]], [_USER_NAME])


class WikiPropertySalesApiGateTests(_OwnerApiTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.sale = baker.make(WikiPropertySale, location=self.location, source=OwnerSource.OFFICIAL)
        self.sale.previous_owners.add(self.official)
        self.sale.new_owners.add(self.contributed)

    def _get(self, raw_key: str):
        return self.client.get(reverse("external_api:wikis.sales", kwargs={"location_slug": self.location.slug}), **_bearer(raw_key))

    def test_a_plain_users_key_never_receives_the_official_partys_name(self) -> None:
        response = self._get(self._key_for(_plain_user()))
        self.assertEqual(response.status_code, 200)
        row = response.json()["results"][0]
        self.assertEqual(row["previous_owners"], [])
        self.assertEqual([owner["name"] for owner in row["new_owners"]], [_USER_NAME])
        self.assertNotIn(_OFFICIAL_NAME, response.content.decode())

    def test_a_subscribers_key_receives_every_party(self) -> None:
        response = self._get(self._key_for(_subscriber()))
        row = response.json()["results"][0]
        self.assertEqual([owner["name"] for owner in row["previous_owners"]], [_OFFICIAL_NAME])
        self.assertEqual([owner["name"] for owner in row["new_owners"]], [_USER_NAME])

    def test_filtering_a_plain_users_view_never_touches_the_stored_m2m_relation(self) -> None:
        """Regression guard: shaping the response must not mutate the database.

        An earlier draft of this fix called ``sale.previous_owners.set(...)``
        to build the filtered response, which would have persisted the
        withheld-for-this-caller list as the sale's *actual* recorded
        parties - destroying the official record for every other viewer too.
        """
        self._get(self._key_for(_plain_user()))
        self.sale.refresh_from_db()
        self.assertEqual(list(self.sale.previous_owners.all()), [self.official])
        self.assertEqual(list(self.sale.new_owners.all()), [self.contributed])
