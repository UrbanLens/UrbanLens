"""P229: a subscriber sees the official owner's contact, the record's former owners and the official sale history.

On the dev stack HRSH's record named "EFG/DRA Heritage LLC" with a mailing address, and the Private Pin page showed the
subscriber the name and nothing else: the Ownership card and Sale History tab there listed only the pin's own notes.
"""

from __future__ import annotations

import datetime
from unittest import mock

from django.contrib.auth.models import User
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import SimpleTestCase, TestCase
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.property_owner.meta import OwnerSource
from urbanlens.dashboard.models.property_owner.model import PinOwner, PinPropertySale, WikiOwner, WikiPropertySale
from urbanlens.dashboard.models.subscriptions.model import SiteFeature, SubscriptionRole, grant_subscription
from urbanlens.dashboard.plugins.builtin.property_records import (
    _fetch_payload,
    _render_available,
    _write_official_owners_and_sales,
)
from urbanlens.dashboard.services.apis.locations.redata_context_gateway import LocationContextEnvelope
from urbanlens.dashboard.services.apis.property_records.redata_gateway import (
    PropertyRecordsUnavailableError,
    RedataGateway,
)

_OWNER = "EFG/DRA Heritage LLC"
_MAILING = "115 Wilcox St Ste 220, Castle Rock, CO 80104"
_GATEWAY = "urbanlens.dashboard.services.apis.property_records.redata_gateway.RedataGateway"


def _plain_user() -> User:
    """A user with no grants; the first user in a fresh database becomes the bootstrap admin, so one is made first."""
    baker.make(User)
    return baker.make(User)


def _subscriber() -> User:
    user = _plain_user()
    role = baker.make(SubscriptionRole, features=SiteFeature.PROPERTY_OWNERS)
    grant_subscription(user, role, user, None)
    return user


def _official(location: Location, name: str = _OWNER, **fields) -> WikiOwner:
    owner = WikiOwner.objects.create(name=name, source=OwnerSource.OFFICIAL, **fields)
    owner.locations.add(location)
    return owner


def _current_names(location: Location) -> set[str]:
    return set(WikiOwner.objects.for_location(location).values_list("name", flat=True))


class OfficialOwnerContactTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.location = baker.make(Location)

    def test_an_owner_first_recorded_without_contact_gains_the_records_mailing_address(self) -> None:
        owner = _official(self.location)

        _write_official_owners_and_sales(self.location, {"owner_name": [_OWNER], "owner_mailing_address": _MAILING})

        owner.refresh_from_db()
        self.assertEqual(owner.address, _MAILING)

    def test_a_changed_official_mailing_address_replaces_the_old_one(self) -> None:
        owner = _official(self.location, address="PO Box 1, Albany, NY")

        _write_official_owners_and_sales(self.location, {"owner_name": [_OWNER], "owner_mailing_address": _MAILING})

        owner.refresh_from_db()
        self.assertEqual(owner.address, _MAILING)

    def test_a_record_with_no_mailing_address_keeps_the_known_one(self) -> None:
        owner = _official(self.location, address=_MAILING)

        _write_official_owners_and_sales(self.location, {"owner_name": [_OWNER]})

        owner.refresh_from_db()
        self.assertEqual(owner.address, _MAILING)

    def test_a_community_owner_of_the_same_name_is_never_rewritten(self) -> None:
        contributed = WikiOwner.objects.create(name=_OWNER, source=OwnerSource.USER, address="Typed by a member")
        contributed.locations.add(self.location)

        _write_official_owners_and_sales(self.location, {"owner_name": [_OWNER], "owner_mailing_address": _MAILING})

        contributed.refresh_from_db()
        self.assertEqual((contributed.address, contributed.source), ("Typed by a member", OwnerSource.USER))

    def test_the_records_care_of_line_is_kept(self) -> None:
        _write_official_owners_and_sales(
            self.location, {"owner_name": [_OWNER], "owner_mailing_address": _MAILING, "owner_care_of": "Jane Agent"}
        )

        self.assertEqual(WikiOwner.objects.for_location(self.location).get().care_of, "Jane Agent")

    def test_redata_owner_records_supply_phone_and_email(self) -> None:
        payload = {
            "owner_name": [_OWNER],
            "owners": [
                {
                    "name": _OWNER,
                    "company_name": "",
                    "mailing_address": _MAILING,
                    "care_of": "",
                    "phone": "555-0100",
                    "email": "office@example.com",
                    "current": True,
                }
            ],
        }

        _write_official_owners_and_sales(self.location, payload)

        owner = WikiOwner.objects.for_location(self.location).get()
        self.assertEqual((owner.address, owner.phone, owner.email), (_MAILING, "555-0100", "office@example.com"))

    def test_an_overlong_name_is_stored_cut_rather_than_failing_the_write(self) -> None:
        name = "A" * 260

        _write_official_owners_and_sales(self.location, {"owner_name": [name], "owner_mailing_address": _MAILING})

        self.assertEqual(WikiOwner.objects.for_location(self.location).get().name, name[:200])


class OfficialCurrentOwnerTests(TestCase):
    """A location's linked owners are its current ones, as the community Sale History form treats them."""

    def setUp(self) -> None:
        super().setUp()
        self.location = baker.make(Location)

    def test_a_seller_is_not_listed_as_a_current_owner(self) -> None:
        payload = {
            "owner_name": ["New Owner"],
            "sales_history": [{"date": "2020-06-15", "price": 250000, "grantor": "Old Owner", "grantee": "New Owner"}],
        }

        _write_official_owners_and_sales(self.location, payload)

        self.assertEqual(_current_names(self.location), {"New Owner"})
        sale = WikiPropertySale.objects.for_location(self.location).get()
        self.assertEqual(list(sale.previous_owners.values_list("name", flat=True)), ["Old Owner"])

    def test_an_owner_the_newest_record_no_longer_names_stops_being_current(self) -> None:
        former = _official(self.location, name="Old LLC")

        _write_official_owners_and_sales(self.location, {"owner_name": ["New LLC"]})

        self.assertEqual(_current_names(self.location), {"New LLC"})
        self.assertTrue(WikiOwner.objects.filter(pk=former.pk).exists())

    def test_a_record_naming_no_owner_leaves_the_current_owners_alone(self) -> None:
        _official(self.location)

        _write_official_owners_and_sales(self.location, {"owner_name": [], "apn": "1-2-3"})

        self.assertEqual(_current_names(self.location), {_OWNER})

    def test_a_community_owner_is_never_unlinked(self) -> None:
        contributed = WikiOwner.objects.create(name="Member's Note", source=OwnerSource.USER)
        contributed.locations.add(self.location)

        _write_official_owners_and_sales(self.location, {"owner_name": ["New LLC"]})

        self.assertEqual(_current_names(self.location), {"Member's Note", "New LLC"})

    def test_a_party_to_two_sales_is_one_owner(self) -> None:
        payload = {
            "owner_name": ["C"],
            "sales_history": [
                {"date": "2010-01-01", "price": 100, "grantor": "A", "grantee": "B"},
                {"date": "2020-01-01", "price": 200, "grantor": "B", "grantee": "C"},
            ],
        }

        _write_official_owners_and_sales(self.location, payload)

        self.assertEqual(WikiOwner.objects.filter(name="B").count(), 1)

    def test_a_party_of_another_locations_sale_is_not_reused(self) -> None:
        elsewhere = baker.make(Location)
        _write_official_owners_and_sales(
            elsewhere, {"sales_history": [{"date": "2010-01-01", "price": 100, "grantor": "A", "grantee": "B"}]}
        )

        _write_official_owners_and_sales(
            self.location, {"sales_history": [{"date": "2012-01-01", "price": 300, "grantor": "A", "grantee": "D"}]}
        )

        self.assertEqual(WikiOwner.objects.filter(name="A").count(), 2)


class FetchOwnerRecordsTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.location = baker.make(Location)

    def _gateway(self, mock_gateway_cls, *, owners=None, sales=None, base=None):
        gateway = mock_gateway_cls.return_value
        gateway.lookup_parcel.return_value = {"uuid": "parcel-1", **(base or {})}
        for name in ("lookup_assessments", "lookup_sale_records"):
            getattr(gateway, name).return_value = LocationContextEnvelope(count=0, complete=True)
        for name in ("lookup_liens", "lookup_tax_payments"):
            getattr(gateway, name).return_value = []
        gateway.lookup_coverage.return_value = {}
        gateway.lookup_demographics.return_value = None
        gateway.lookup_national_parks.return_value = {}
        gateway.lookup_owners.return_value = owners or []
        gateway.lookup_sales.return_value = sales or []
        return gateway

    def test_redata_s_owner_records_are_kept_without_client_entered_ones(self) -> None:
        rows = [
            {
                "id": 8127,
                "parcels": [289, 205],
                "name": _OWNER,
                "company_name": "",
                "mailing_address": _MAILING,
                "care_of": "",
                "phone": "",
                "email": "",
                "source": "official",
                "first_observed_at": "2026-09-20",
                "last_observed_at": "2026-10-04",
                "current": True,
            },
            {"id": 9, "parcels": [289], "name": "Somebody's Guess", "source": "manual", "current": None},
            {
                "id": 11,
                "parcels": [289],
                "name": "Hudson Valley DDSO",
                "source": "derived",
                "first_observed_at": "1990-01-01",
                "last_observed_at": "2005-06-30",
                "current": False,
            },
        ]
        with mock.patch(_GATEWAY) as gateway_cls:
            self._gateway(gateway_cls, owners=rows)
            payload = _fetch_payload(self.location, 41.7, -73.9)

        owners = {owner["name"]: owner for owner in payload["owners"]}
        self.assertEqual(set(owners), {_OWNER, "Hudson Valley DDSO"})
        self.assertEqual(owners[_OWNER]["mailing_address"], _MAILING)
        self.assertEqual(owners[_OWNER]["other_parcels"], 1)
        self.assertIs(owners["Hudson Valley DDSO"]["current"], False)
        self.assertEqual(owners["Hudson Valley DDSO"]["last_observed"], "2005-06-30")

    def test_redata_s_recorded_sales_join_the_records_sales(self) -> None:
        recorded = [
            {
                "sale_price": "460000.00",
                "sale_date": "2024-03-15",
                "grantor": "John Smith",
                "grantee": "Jane Doe",
                "doc_type": "WARRANTY DEED",
                "doc_number": "1234/567",
                "source": "official",
            },
            {"sale_price": "1.00", "sale_date": "2001-01-01", "grantor": "X", "grantee": "Y", "source": "manual"},
        ]
        base = {"sales_history": [{"date": "2024-03-15", "price": 460000, "grantor": "", "grantee": ""}]}
        with mock.patch(_GATEWAY) as gateway_cls:
            self._gateway(gateway_cls, sales=recorded, base=base)
            payload = _fetch_payload(self.location, 41.7, -73.9)

        self.assertEqual(len(payload["sales_history"]), 1)
        sale = payload["sales_history"][0]
        self.assertEqual(
            (sale["grantor"], sale["grantee"], sale["doc_type"]), ("John Smith", "Jane Doe", "WARRANTY DEED")
        )

    def test_a_failed_owner_or_sale_lookup_leaves_the_record(self) -> None:
        with mock.patch(_GATEWAY) as gateway_cls:
            gateway = self._gateway(gateway_cls)
            gateway.lookup_owners.side_effect = PropertyRecordsUnavailableError("source_error", "down")
            gateway.lookup_sales.side_effect = PropertyRecordsUnavailableError("source_error", "down")
            payload = _fetch_payload(self.location, 41.7, -73.9)

        self.assertTrue(payload["available"])
        self.assertNotIn("owners", payload)


class GatewayOwnerEndpointsTests(SimpleTestCase):
    def test_owners_and_sales_read_the_parcels_own_lists(self) -> None:
        gateway = RedataGateway.__new__(RedataGateway)
        with mock.patch.object(RedataGateway, "_get_json", return_value={"results": [{"name": _OWNER}]}) as get_json:
            self.assertEqual(gateway.lookup_owners("p-1"), [{"name": _OWNER}])
            self.assertEqual(gateway.lookup_sales("p-1"), [{"name": _OWNER}])

        self.assertEqual(
            [call.args[0] for call in get_json.call_args_list],
            ["/api/v1/parcels/p-1/owners/", "/api/v1/parcels/p-1/sales/"],
        )


class ParcelCardOwnerDetailsTests(SimpleTestCase):
    _DATA = {
        "available": True,
        "owner_name": [_OWNER],
        "owner_mailing_address": _MAILING,
        "owner_care_of": "Jane Agent",
        "owners": [
            {"name": _OWNER, "mailing_address": _MAILING, "current": True, "other_parcels": 1},
            {"name": "Hudson Valley DDSO", "current": False, "last_observed": "2005-06-30", "other_parcels": 0},
        ],
    }

    def _meta(self, *, show_owner: bool) -> dict[str, str]:
        context = _render_available(self._DATA, show_owner=show_owner, show_demographics=False)
        return {row["label"]: str(row["value"]) for row in context["meta"]}

    def test_an_entitled_viewer_reads_the_owners_mailing_address_and_care_of(self) -> None:
        meta = self._meta(show_owner=True)

        self.assertEqual(meta["Owner mailing address"], _MAILING)
        self.assertEqual(meta["Care of"], "Jane Agent")

    def test_an_entitled_viewer_reads_the_former_owners_and_other_parcels(self) -> None:
        meta = self._meta(show_owner=True)

        self.assertIn("Hudson Valley DDSO", meta["Former owner"])
        self.assertIn("2005", meta["Former owner"])
        self.assertIn("1 other parcel", meta["Owner's other parcels"])

    def test_nothing_about_the_owner_reaches_a_viewer_not_entitled_to_it(self) -> None:
        context = _render_available(self._DATA, show_owner=False, show_demographics=False)

        rendered = repr(context)
        for secret in (_OWNER, _MAILING, "Jane Agent", "Hudson Valley DDSO", "other parcel"):
            self.assertNotIn(secret, rendered)


class PinPageOfficialOwnershipTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.location = baker.make(Location, latitude="41.7", longitude="-73.9")
        self.official = _official(self.location, address=_MAILING, care_of="Jane Agent")
        self.sale = WikiPropertySale.objects.create(
            location=self.location, source=OwnerSource.OFFICIAL, sale_date=datetime.date(2020, 6, 15), sale_price=250000
        )
        self.seller = WikiOwner.objects.create(name="Previous Holder", source=OwnerSource.OFFICIAL)
        self.sale.previous_owners.add(self.seller)
        self.sale.new_owners.add(self.official)

    def _login(self, user: User) -> Pin:
        self.client.force_login(user)
        return baker.make(Pin, profile=user.profile, location=self.location, name="Mine")

    def test_a_subscriber_sees_the_official_owner_with_contact_on_their_pin(self) -> None:
        pin = self._login(_subscriber())

        response = self.client.get(reverse("pin.ownership", args=[pin.slug]))

        self.assertContains(response, _OWNER)
        self.assertContains(response, _MAILING)
        self.assertContains(response, "Jane Agent")
        self.assertNotContains(response, reverse("pin.ownership.edit", args=[pin.slug, self.official.pk]))

    def test_a_plain_user_is_told_an_owner_is_on_record_without_it(self) -> None:
        pin = self._login(_plain_user())

        response = self.client.get(reverse("pin.ownership", args=[pin.slug]))

        self.assertNotContains(response, _OWNER)
        self.assertNotContains(response, _MAILING)
        self.assertContains(response, "1 official owner record")

    def test_a_card_holding_only_a_withheld_owner_is_not_collapsed_as_empty(self) -> None:
        """An empty card hides itself whole, which hid the line saying an owner is on record."""
        pin = self._login(_plain_user())

        response = self.client.get(reverse("pin.ownership", args=[pin.slug]))

        self.assertContains(response, 'data-collapse-if-empty=""')

    def test_the_pins_own_notes_still_show_beside_the_official_owner(self) -> None:
        pin = self._login(_subscriber())
        PinOwner.objects.create(pin=pin, name="My Landlord Note")

        response = self.client.get(reverse("pin.ownership", args=[pin.slug]))

        self.assertContains(response, "My Landlord Note")
        self.assertContains(response, _OWNER)

    def test_a_subscriber_sees_the_official_sale_with_its_parties(self) -> None:
        pin = self._login(_subscriber())

        response = self.client.get(reverse("pin.sales", args=[pin.slug]))

        self.assertContains(response, "250000")
        self.assertContains(response, "Previous Holder")
        self.assertContains(response, _OWNER)

    def test_a_plain_user_sees_the_official_sale_but_not_its_parties(self) -> None:
        pin = self._login(_plain_user())

        response = self.client.get(reverse("pin.sales", args=[pin.slug]))

        self.assertContains(response, "250000")
        self.assertNotContains(response, "Previous Holder")
        self.assertContains(response, "Subscribers only")

    def test_an_official_sale_offers_no_delete_on_the_pin(self) -> None:
        pin = self._login(_subscriber())
        own = PinPropertySale.objects.create(pin=pin, sale_date=datetime.date(2021, 1, 1))
        if own.pk == self.sale.pk:
            own.delete()
            own = PinPropertySale.objects.create(pin=pin, sale_date=datetime.date(2021, 1, 1))

        response = self.client.get(reverse("pin.sales", args=[pin.slug]))

        self.assertContains(response, reverse("pin.sales.delete", args=[pin.slug, own.pk]))
        self.assertNotContains(response, reverse("pin.sales.delete", args=[pin.slug, self.sale.pk]))

    def test_an_owner_typed_on_the_pin_keeps_its_care_of_line(self) -> None:
        pin = self._login(_plain_user())

        self.client.post(reverse("pin.ownership", args=[pin.slug]), {"name": "My Landlord", "care_of": "Their Lawyer"})

        self.assertEqual(PinOwner.objects.get(pin=pin).care_of, "Their Lawyer")
