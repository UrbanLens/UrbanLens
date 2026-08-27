"""Tests for the subscriber gate on officially-sourced property owner data.

An owner record names a private individual and often carries their mailing
address. Where it came from decides who may see it: records UrbanLens looked
up *for* the user from county assessor data (``OwnerSource.OFFICIAL``, via
REData's paid property-records feed) need
``SiteFeature.PROPERTY_OWNERS``; a user's own ``PinOwner`` notes and
community-typed ``WikiOwner`` rows do not.

The filtering is asserted at the service layer *and* through the rendered
panels, because "withheld" has to mean the name never reaches the response -
not that it is present in the HTML and hidden.
"""

from __future__ import annotations

from django.contrib.auth.models import User
from django.test import Client
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.property_owner.meta import OwnerSource
from urbanlens.dashboard.models.property_owner.model import PinOwner, WikiOwner, WikiPropertySale
from urbanlens.dashboard.models.subscriptions.model import SiteFeature, SubscriptionRole, grant_subscription
from urbanlens.dashboard.services.property.owner_access import (
    can_see_official_owners,
    sale_rows,
    visible_owners,
    withheld_official_count,
)

_OFFICIAL_NAME = "Hudson Heritage LLC"
_USER_NAME = "Community Contributed Owner"


def _plain_user() -> User:
    """A user with no subscription and no feature grants.

    The very first user created in a fresh test database is auto-promoted to
    bootstrap site admin, and ``user_has_feature`` grants a site admin every
    feature - so a throwaway user absorbs that promotion and the user under
    test is an ordinary one. Same precedent as ``test_panel_feature_gate.py``.
    """
    baker.make(User)
    return baker.make(User)


def _subscriber() -> User:
    """A user holding an active role that grants the property-owners feature."""
    user = _plain_user()
    role = baker.make(SubscriptionRole, features=SiteFeature.PROPERTY_OWNERS)
    grant_subscription(user, role, user, None)
    return user


class OwnerAccessServiceTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.location = baker.make(Location)
        self.official = baker.make(WikiOwner, name=_OFFICIAL_NAME, source=OwnerSource.OFFICIAL, address="1 Main St")
        self.official.locations.add(self.location)
        self.contributed = baker.make(WikiOwner, name=_USER_NAME, source=OwnerSource.USER)
        self.contributed.locations.add(self.location)
        self.owners = [self.official, self.contributed]

    def test_a_plain_user_sees_only_contributed_owners(self) -> None:
        names = [owner.name for owner in visible_owners(self.owners, _plain_user())]
        self.assertEqual(names, [_USER_NAME])

    def test_a_subscriber_sees_both(self) -> None:
        names = {owner.name for owner in visible_owners(self.owners, _subscriber())}
        self.assertEqual(names, {_OFFICIAL_NAME, _USER_NAME})

    def test_withheld_count_reports_what_was_hidden(self) -> None:
        self.assertEqual(withheld_official_count(self.owners, _plain_user()), 1)
        self.assertEqual(withheld_official_count(self.owners, _subscriber()), 0)

    def test_anonymous_users_see_no_official_records(self) -> None:
        from django.contrib.auth.models import AnonymousUser

        self.assertFalse(can_see_official_owners(AnonymousUser()))
        self.assertEqual([owner.name for owner in visible_owners(self.owners, AnonymousUser())], [_USER_NAME])

    def test_pin_owners_pass_through_untouched(self) -> None:
        """PinOwner has no ``source`` at all - private records are always the user's own."""
        pin = baker.make_recipe("dashboard.pin", profile=baker.make(User).profile)
        mine = baker.make(PinOwner, pin=pin, name="My Own Note")
        self.assertEqual([owner.name for owner in visible_owners([mine], _plain_user())], ["My Own Note"])


class SaleRowAccessTests(TestCase):
    """A deed's grantor/grantee are owner records too - gating the Ownership
    panel while leaving Sale History open would hand back the same names."""

    def setUp(self) -> None:
        super().setUp()
        self.location = baker.make(Location)
        self.official = baker.make(WikiOwner, name=_OFFICIAL_NAME, source=OwnerSource.OFFICIAL)
        self.contributed = baker.make(WikiOwner, name=_USER_NAME, source=OwnerSource.USER)
        self.sale = baker.make(WikiPropertySale, location=self.location, source=OwnerSource.OFFICIAL)
        self.sale.previous_owners.add(self.official)
        self.sale.new_owners.add(self.contributed)

    def test_official_parties_are_withheld_from_a_plain_user(self) -> None:
        rows = sale_rows([self.sale], _plain_user())
        self.assertEqual([owner.name for owner in rows[0]["previous_owners"]], [])
        self.assertEqual([owner.name for owner in rows[0]["new_owners"]], [_USER_NAME])
        self.assertTrue(rows[0]["parties_withheld"])

    def test_a_subscriber_sees_every_party(self) -> None:
        rows = sale_rows([self.sale], _subscriber())
        self.assertEqual([owner.name for owner in rows[0]["previous_owners"]], [_OFFICIAL_NAME])
        self.assertFalse(rows[0]["parties_withheld"])


class WikiOwnershipPanelGateTests(TestCase):
    """End-to-end: the withheld name must not appear in the response body."""

    def setUp(self) -> None:
        super().setUp()
        self.client = Client()
        self.location = baker.make(Location)
        official = baker.make(WikiOwner, name=_OFFICIAL_NAME, source=OwnerSource.OFFICIAL, address="1 Main St", phone="555-0100")
        official.locations.add(self.location)
        contributed = baker.make(WikiOwner, name=_USER_NAME, source=OwnerSource.USER)
        contributed.locations.add(self.location)

    def _get_panel(self, user: User):
        """Fetch the wiki Ownership panel as ``user``, who is given a pin here."""
        baker.make_recipe("dashboard.pin", profile=user.profile, location=self.location)
        baker.make("dashboard.Wiki", location=self.location)
        self.client.force_login(user)
        return self.client.get(reverse("location.wiki.ownership", args=[self.location.slug]))

    def test_plain_user_never_receives_the_official_name_or_contact(self) -> None:
        response = self._get_panel(_plain_user())
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, _OFFICIAL_NAME)
        self.assertNotContains(response, "555-0100")
        self.assertContains(response, _USER_NAME)

    def test_plain_user_is_told_records_exist(self) -> None:
        """An empty card would read as "no owner on record" - a different claim."""
        response = self._get_panel(_plain_user())
        self.assertContains(response, "official owner record")

    def test_subscriber_receives_the_official_record(self) -> None:
        response = self._get_panel(_subscriber())
        self.assertContains(response, _OFFICIAL_NAME)
        self.assertNotContains(response, "official owner record")


class PropertyRecordsCardGateTests(TestCase):
    """The card's heading is the owner's name; its tax/parcel facts are not gated."""

    def setUp(self) -> None:
        super().setUp()
        from urbanlens.dashboard.plugins.builtin.property_records import PropertyRecordsPanelSource

        self.source = PropertyRecordsPanelSource()
        self.data = {
            "available": True,
            "owner_name": [_OFFICIAL_NAME],
            "situs_address": "83 Hudson View Dr",
            "apn": "1234-56",
        }

    def _pin_for(self, user: User):
        return baker.make_recipe("dashboard.pin", profile=user.profile)

    def test_plain_user_gets_no_owner_heading(self) -> None:
        context = self.source.render_context(self._pin_for(_plain_user()), self.data)
        assert context is not None
        self.assertIsNone(context["heading_name"])

    def test_plain_user_still_gets_the_parcel_facts(self) -> None:
        context = self.source.render_context(self._pin_for(_plain_user()), self.data)
        assert context is not None
        labels = {entry["label"]: entry["value"] for entry in context["meta"]}
        self.assertEqual(labels["Address"], "83 Hudson View Dr")
        self.assertEqual(labels["APN / Parcel ID"], "1234-56")

    def test_plain_user_is_told_an_owner_is_on_record(self) -> None:
        context = self.source.render_context(self._pin_for(_plain_user()), self.data)
        assert context is not None
        self.assertIn("Owner on record - subscribers only", context["chips"])

    def test_subscriber_gets_the_owner_heading(self) -> None:
        context = self.source.render_context(self._pin_for(_subscriber()), self.data)
        assert context is not None
        self.assertEqual(context["heading_name"], _OFFICIAL_NAME)
        self.assertNotIn("Owner on record - subscribers only", context["chips"])

    def test_a_record_with_no_owner_gets_no_chip(self) -> None:
        """The chip claims a name exists - it must not appear when none does."""
        context = self.source.render_context(self._pin_for(_plain_user()), {"available": True, "apn": "1234-56"})
        assert context is not None
        self.assertNotIn("Owner on record - subscribers only", context["chips"])
