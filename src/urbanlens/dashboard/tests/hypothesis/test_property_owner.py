"""Tests for the Ownership card: owner CRUD, sale history, and the pin-ownership permission gate."""

from __future__ import annotations

import datetime
from typing import TYPE_CHECKING

from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.models.property_owner.model import Owner, PropertySale

if TYPE_CHECKING:
    from django.contrib.auth.models import User


class OwnerModelTests(TestCase):
    """Model-level behavior: multi-location association and sale ordering."""

    def test_owner_can_be_linked_to_multiple_locations(self) -> None:
        owner = baker.make(Owner, name="Acme Holdings")
        location_a = baker.make(Location, latitude="41.0", longitude="-73.0")
        location_b = baker.make(Location, latitude="42.0", longitude="-74.0")
        owner.locations.add(location_a, location_b)
        self.assertCountEqual(Owner.objects.for_location(location_a), [owner])
        self.assertCountEqual(Owner.objects.for_location(location_b), [owner])

    def test_property_sale_queryset_orders_newest_first(self) -> None:
        location = baker.make(Location, latitude="41.0", longitude="-73.0")
        older = baker.make(PropertySale, location=location, sale_date=datetime.date(2020, 1, 1))
        newer = baker.make(PropertySale, location=location, sale_date=datetime.date(2024, 1, 1))
        self.assertEqual(list(PropertySale.objects.for_location(location)), [newer, older])

    def test_deleting_owner_referenced_by_a_sale_nulls_the_fk_not_the_sale(self) -> None:
        location = baker.make(Location, latitude="41.0", longitude="-73.0")
        owner = baker.make(Owner, name="Old Owner")
        sale = baker.make(PropertySale, location=location, previous_owner=owner)
        owner.delete()
        sale.refresh_from_db()
        self.assertIsNone(sale.previous_owner_id)


class OwnershipPanelViewTestsBase(TestCase):
    """Shared fixture: a logged-in user with a pin at a location."""

    def setUp(self) -> None:
        baker.make("auth.User")  # first user is auto-promoted to bootstrap site admin
        self.user: User = baker.make("auth.User")
        self.profile: Profile = Profile.objects.get(user=self.user)
        self.location = baker.make(Location, latitude="41.400000", longitude="-73.400000")
        self.pin = baker.make(Pin, profile=self.profile, location=self.location, name="My Pin")
        self.client.force_login(self.user)


class OwnershipPanelViewTests(OwnershipPanelViewTestsBase):
    """GET/POST on the Ownership card itself."""

    def test_get_renders_owners_for_the_pins_location(self) -> None:
        owner = baker.make(Owner, name="Diverset Corp")
        owner.locations.add(self.location)
        response = self.client.get(reverse("pin.ownership", args=[self.pin.slug]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Diverset Corp")

    def test_post_creates_an_owner_linked_to_the_location(self) -> None:
        response = self.client.post(
            reverse("pin.ownership", args=[self.pin.slug]),
            {"name": "New Owner LLC", "company_name": "New Owner LLC", "email": "contact@example.com"},
        )
        self.assertEqual(response.status_code, 200)
        owner = Owner.objects.get(name="New Owner LLC")
        self.assertIn(self.location, owner.locations.all())

    def test_post_without_a_name_is_rejected(self) -> None:
        response = self.client.post(reverse("pin.ownership", args=[self.pin.slug]), {"name": "  "})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Owner.objects.exists())

    def test_viewing_a_pin_you_dont_own_404s(self) -> None:
        other_pin = baker.make(Pin, profile=baker.make("auth.User").profile, location=self.location)
        response = self.client.get(reverse("pin.ownership", args=[other_pin.slug]))
        self.assertEqual(response.status_code, 404)


class OwnerUpdateViewTests(OwnershipPanelViewTestsBase):
    """POST to edit an existing owner."""

    def test_updates_owner_fields(self) -> None:
        owner = baker.make(Owner, name="Old Name")
        owner.locations.add(self.location)
        response = self.client.post(
            reverse("pin.ownership.edit", args=[self.pin.slug, owner.id]),
            {"name": "New Name", "phone": "555-1234"},
        )
        self.assertEqual(response.status_code, 200)
        owner.refresh_from_db()
        self.assertEqual(owner.name, "New Name")
        self.assertEqual(owner.phone, "555-1234")

    def test_editing_an_owner_not_linked_to_this_location_404s(self) -> None:
        unrelated_location = baker.make(Location, latitude="10.0", longitude="10.0")
        owner = baker.make(Owner, name="Elsewhere Owner")
        owner.locations.add(unrelated_location)
        response = self.client.post(reverse("pin.ownership.edit", args=[self.pin.slug, owner.id]), {"name": "Hijacked"})
        self.assertEqual(response.status_code, 404)


class OwnerRemoveViewTests(OwnershipPanelViewTestsBase):
    """DELETE to unlink an owner from a property."""

    def test_unlinks_owner_but_keeps_the_record_if_linked_elsewhere(self) -> None:
        other_location = baker.make(Location, latitude="10.0", longitude="10.0")
        owner = baker.make(Owner, name="Multi Property Owner")
        owner.locations.add(self.location, other_location)
        response = self.client.delete(reverse("pin.ownership.remove", args=[self.pin.slug, owner.id]))
        self.assertEqual(response.status_code, 200)
        owner.refresh_from_db()
        self.assertNotIn(self.location, owner.locations.all())
        self.assertIn(other_location, owner.locations.all())
        self.assertTrue(Owner.objects.filter(pk=owner.pk).exists())

    def test_unlinking_the_only_location_keeps_the_owner_record(self) -> None:
        owner = baker.make(Owner, name="Solo Property Owner")
        owner.locations.add(self.location)
        self.client.delete(reverse("pin.ownership.remove", args=[self.pin.slug, owner.id]))
        self.assertTrue(Owner.objects.filter(pk=owner.pk).exists())


class PropertySaleTabViewTests(OwnershipPanelViewTestsBase):
    """GET/POST on the Sale History tab."""

    def test_get_renders_sales_for_the_pins_location(self) -> None:
        baker.make(PropertySale, location=self.location, sale_price="450000.00", notes="Cash sale")
        response = self.client.get(reverse("pin.sales", args=[self.pin.slug]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Cash sale")

    def test_post_creates_a_sale_and_get_or_creates_named_owners(self) -> None:
        response = self.client.post(
            reverse("pin.sales", args=[self.pin.slug]),
            {"previous_owner": "Seller Co", "new_owner": "Buyer Co", "sale_price": "300000", "sale_date": "2024-06-01"},
        )
        self.assertEqual(response.status_code, 200)
        sale = PropertySale.objects.get(location=self.location)
        self.assertEqual(sale.previous_owner.name, "Seller Co")
        self.assertEqual(sale.new_owner.name, "Buyer Co")
        self.assertEqual(str(sale.sale_price), "300000.00")

    def test_recording_a_sale_removes_the_previous_owner_from_current_owners(self) -> None:
        seller = baker.make(Owner, name="Seller Co")
        seller.locations.add(self.location)
        self.client.post(reverse("pin.sales", args=[self.pin.slug]), {"previous_owner": "Seller Co", "new_owner": "Buyer Co"})
        seller.refresh_from_db()
        self.assertNotIn(self.location, seller.locations.all())
        buyer = Owner.objects.get(name="Buyer Co")
        self.assertIn(self.location, buyer.locations.all())

    def test_same_previous_and_new_owner_is_rejected(self) -> None:
        response = self.client.post(reverse("pin.sales", args=[self.pin.slug]), {"previous_owner": "Same Co", "new_owner": "Same Co"})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(PropertySale.objects.exists())

    def test_invalid_sale_price_is_rejected(self) -> None:
        response = self.client.post(reverse("pin.sales", args=[self.pin.slug]), {"sale_price": "not-a-number"})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(PropertySale.objects.exists())

    def test_negative_sale_price_is_rejected(self) -> None:
        response = self.client.post(reverse("pin.sales", args=[self.pin.slug]), {"sale_price": "-100"})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(PropertySale.objects.exists())

    def test_invalid_sale_date_is_rejected(self) -> None:
        response = self.client.post(reverse("pin.sales", args=[self.pin.slug]), {"sale_date": "not-a-date"})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(PropertySale.objects.exists())


class PropertySaleDeleteViewTests(OwnershipPanelViewTestsBase):
    """DELETE a sale record."""

    def test_deletes_the_sale(self) -> None:
        sale = baker.make(PropertySale, location=self.location)
        response = self.client.delete(reverse("pin.sales.delete", args=[self.pin.slug, sale.id]))
        self.assertEqual(response.status_code, 200)
        self.assertFalse(PropertySale.objects.filter(pk=sale.pk).exists())

    def test_deleting_a_sale_for_another_location_404s(self) -> None:
        unrelated_location = baker.make(Location, latitude="10.0", longitude="10.0")
        sale = baker.make(PropertySale, location=unrelated_location)
        response = self.client.delete(reverse("pin.sales.delete", args=[self.pin.slug, sale.id]))
        self.assertEqual(response.status_code, 404)
