"""Tests for the Ownership card: private-vs-shared visibility, official-data locking,
multi-owner sales, and the pin-ownership permission gate."""

from __future__ import annotations

import datetime
from typing import TYPE_CHECKING

from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.models.property_owner.meta import OwnerSource, OwnerVisibility
from urbanlens.dashboard.models.property_owner.model import Owner, PropertySale

if TYPE_CHECKING:
    from django.contrib.auth.models import User


class OwnerModelTests(TestCase):
    """Model-level behavior: shared multi-location association, sale ordering, visibility scoping."""

    def test_shared_owner_can_be_linked_to_multiple_locations(self) -> None:
        owner = baker.make(Owner, name="Acme Holdings", visibility=OwnerVisibility.SHARED)
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

    def test_for_location_excludes_private_owners(self) -> None:
        location = baker.make(Location, latitude="41.0", longitude="-73.0")
        pin = baker.make(Pin, location=location)
        private_owner = baker.make(Owner, name="Private Owner", visibility=OwnerVisibility.PRIVATE)
        private_owner.pins.add(pin)
        self.assertCountEqual(Owner.objects.for_location(location), [])

    def test_deleting_owner_referenced_by_a_sale_removes_it_from_that_sale_only(self) -> None:
        location = baker.make(Location, latitude="41.0", longitude="-73.0")
        owner = baker.make(Owner, name="Old Owner")
        sale = baker.make(PropertySale, location=location)
        sale.previous_owners.add(owner)
        owner.delete()
        self.assertCountEqual(sale.previous_owners.all(), [])
        self.assertTrue(PropertySale.objects.filter(pk=sale.pk).exists())


class OwnershipPanelViewTestsBase(TestCase):
    """Shared fixture: two profiles, each with their own pin at the same shared location."""

    def setUp(self) -> None:
        baker.make("auth.User")  # first user is auto-promoted to bootstrap site admin
        self.user: User = baker.make("auth.User")
        self.profile: Profile = Profile.objects.get(user=self.user)
        self.location = baker.make(Location, latitude="41.400000", longitude="-73.400000")
        self.pin = baker.make(Pin, profile=self.profile, location=self.location, name="My Pin")
        self.client.force_login(self.user)

        self.other_user: User = baker.make("auth.User")
        self.other_profile: Profile = Profile.objects.get(user=self.other_user)
        self.other_pin = baker.make(Pin, profile=self.other_profile, location=self.location, name="Their Pin")


class OwnershipPanelViewTests(OwnershipPanelViewTestsBase):
    """GET/POST on the Ownership card itself."""

    def test_get_renders_shared_owners_for_the_pins_location(self) -> None:
        owner = baker.make(Owner, name="Diverset Corp", visibility=OwnerVisibility.SHARED)
        owner.locations.add(self.location)
        response = self.client.get(reverse("pin.ownership", args=[self.pin.slug]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Diverset Corp")

    def test_post_defaults_to_a_shared_owner(self) -> None:
        response = self.client.post(reverse("pin.ownership", args=[self.pin.slug]), {"name": "New Owner LLC"})
        self.assertEqual(response.status_code, 200)
        owner = Owner.objects.get(name="New Owner LLC")
        self.assertEqual(owner.visibility, OwnerVisibility.SHARED)
        self.assertIn(self.location, owner.locations.all())

    def test_shared_owner_is_visible_on_a_different_profiles_pin_at_the_same_location(self) -> None:
        self.client.post(reverse("pin.ownership", args=[self.pin.slug]), {"name": "Community Owner"})
        self.client.force_login(self.other_user)
        response = self.client.get(reverse("pin.ownership", args=[self.other_pin.slug]))
        self.assertContains(response, "Community Owner")

    def test_post_with_private_checkbox_creates_a_private_owner(self) -> None:
        response = self.client.post(
            reverse("pin.ownership", args=[self.pin.slug]),
            {"name": "My Secret Landlord", "visibility": "private"},
        )
        self.assertEqual(response.status_code, 200)
        owner = Owner.objects.get(name="My Secret Landlord")
        self.assertEqual(owner.visibility, OwnerVisibility.PRIVATE)
        self.assertIn(self.pin, owner.pins.all())
        self.assertFalse(owner.locations.exists())

    def test_private_owner_is_not_visible_on_another_pin_at_the_same_location(self) -> None:
        self.client.post(reverse("pin.ownership", args=[self.pin.slug]), {"name": "My Secret Landlord", "visibility": "private"})
        self.client.force_login(self.other_user)
        response = self.client.get(reverse("pin.ownership", args=[self.other_pin.slug]))
        self.assertNotContains(response, "My Secret Landlord")

    def test_private_owner_is_visible_on_the_pin_it_was_added_to(self) -> None:
        self.client.post(reverse("pin.ownership", args=[self.pin.slug]), {"name": "My Secret Landlord", "visibility": "private"})
        response = self.client.get(reverse("pin.ownership", args=[self.pin.slug]))
        self.assertContains(response, "My Secret Landlord")

    def test_post_without_a_name_is_rejected(self) -> None:
        response = self.client.post(reverse("pin.ownership", args=[self.pin.slug]), {"name": "  "})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Owner.objects.exists())

    def test_viewing_a_pin_you_dont_own_404s(self) -> None:
        response = self.client.get(reverse("pin.ownership", args=[self.other_pin.slug]))
        self.assertEqual(response.status_code, 404)


class OwnerUpdateViewTests(OwnershipPanelViewTestsBase):
    """POST to edit an existing owner."""

    def test_updates_shared_owner_fields(self) -> None:
        owner = baker.make(Owner, name="Old Name", visibility=OwnerVisibility.SHARED)
        owner.locations.add(self.location)
        response = self.client.post(
            reverse("pin.ownership.edit", args=[self.pin.slug, owner.id]),
            {"name": "New Name", "phone": "555-1234"},
        )
        self.assertEqual(response.status_code, 200)
        owner.refresh_from_db()
        self.assertEqual(owner.name, "New Name")
        self.assertEqual(owner.phone, "555-1234")

    def test_editing_a_private_owner_belonging_to_another_pin_here_404s(self) -> None:
        owner = baker.make(Owner, name="Their Private Owner", visibility=OwnerVisibility.PRIVATE)
        owner.pins.add(self.other_pin)
        response = self.client.post(reverse("pin.ownership.edit", args=[self.pin.slug, owner.id]), {"name": "Hijacked"})
        self.assertEqual(response.status_code, 404)

    def test_official_owner_cannot_be_edited(self) -> None:
        owner = baker.make(Owner, name="Official Records Inc", visibility=OwnerVisibility.SHARED, source=OwnerSource.OFFICIAL)
        owner.locations.add(self.location)
        response = self.client.post(reverse("pin.ownership.edit", args=[self.pin.slug, owner.id]), {"name": "Tampered Name"})
        self.assertEqual(response.status_code, 200)
        owner.refresh_from_db()
        self.assertEqual(owner.name, "Official Records Inc")


class OwnerRemoveViewTests(OwnershipPanelViewTestsBase):
    """DELETE to unlink an owner from a property."""

    def test_unlinks_shared_owner_but_keeps_the_record_if_linked_elsewhere(self) -> None:
        other_location = baker.make(Location, latitude="10.0", longitude="10.0")
        owner = baker.make(Owner, name="Multi Property Owner", visibility=OwnerVisibility.SHARED)
        owner.locations.add(self.location, other_location)
        response = self.client.delete(reverse("pin.ownership.remove", args=[self.pin.slug, owner.id]))
        self.assertEqual(response.status_code, 200)
        owner.refresh_from_db()
        self.assertNotIn(self.location, owner.locations.all())
        self.assertIn(other_location, owner.locations.all())
        self.assertTrue(Owner.objects.filter(pk=owner.pk).exists())

    def test_unlinking_a_private_owners_only_pin_keeps_the_record(self) -> None:
        owner = baker.make(Owner, name="Solo Private Owner", visibility=OwnerVisibility.PRIVATE)
        owner.pins.add(self.pin)
        self.client.delete(reverse("pin.ownership.remove", args=[self.pin.slug, owner.id]))
        self.assertTrue(Owner.objects.filter(pk=owner.pk).exists())
        self.assertFalse(owner.pins.exists())

    def test_official_owner_cannot_be_removed(self) -> None:
        owner = baker.make(Owner, name="Official Records Inc", visibility=OwnerVisibility.SHARED, source=OwnerSource.OFFICIAL)
        owner.locations.add(self.location)
        response = self.client.delete(reverse("pin.ownership.remove", args=[self.pin.slug, owner.id]))
        self.assertEqual(response.status_code, 200)
        owner.refresh_from_db()
        self.assertIn(self.location, owner.locations.all())


class PropertySaleTabViewTests(OwnershipPanelViewTestsBase):
    """GET/POST on the Sale History tab."""

    def test_get_renders_shared_sales_for_the_pins_location(self) -> None:
        baker.make(PropertySale, location=self.location, sale_price="450000.00", notes="Cash sale")
        response = self.client.get(reverse("pin.sales", args=[self.pin.slug]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Cash sale")

    def test_private_sale_not_visible_on_another_pin_at_the_same_location(self) -> None:
        self.client.post(reverse("pin.sales", args=[self.pin.slug]), {"notes": "Secret deal", "visibility": "private"})
        self.client.force_login(self.other_user)
        response = self.client.get(reverse("pin.sales", args=[self.other_pin.slug]))
        self.assertNotContains(response, "Secret deal")

    def test_post_creates_a_sale_with_multiple_previous_and_new_owners(self) -> None:
        response = self.client.post(
            reverse("pin.sales", args=[self.pin.slug]),
            {
                "previous_owners": "Seller One, Seller Two",
                "new_owners": "Buyer One, Buyer Two, Buyer Three",
                "sale_price": "300000",
                "sale_date": "2024-06-01",
            },
        )
        self.assertEqual(response.status_code, 200)
        sale = PropertySale.objects.get(location=self.location)
        self.assertCountEqual(sale.previous_owners.values_list("name", flat=True), ["Seller One", "Seller Two"])
        self.assertCountEqual(sale.new_owners.values_list("name", flat=True), ["Buyer One", "Buyer Two", "Buyer Three"])
        self.assertEqual(str(sale.sale_price), "300000.00")

    def test_recording_a_shared_sale_removes_previous_owners_from_current_shared_owners(self) -> None:
        seller = baker.make(Owner, name="Seller Co", visibility=OwnerVisibility.SHARED)
        seller.locations.add(self.location)
        self.client.post(reverse("pin.sales", args=[self.pin.slug]), {"previous_owners": "Seller Co", "new_owners": "Buyer Co"})
        seller.refresh_from_db()
        self.assertNotIn(self.location, seller.locations.all())
        buyer = Owner.objects.get(name="Buyer Co")
        self.assertIn(self.location, buyer.locations.all())

    def test_recording_a_private_sale_removes_previous_owner_from_private_owners_only(self) -> None:
        seller = baker.make(Owner, name="Seller Co", visibility=OwnerVisibility.PRIVATE)
        seller.pins.add(self.pin)
        self.client.post(
            reverse("pin.sales", args=[self.pin.slug]),
            {"previous_owners": "Seller Co", "new_owners": "Buyer Co", "visibility": "private"},
        )
        seller.refresh_from_db()
        self.assertNotIn(self.pin, seller.pins.all())
        buyer = Owner.objects.get(name="Buyer Co", visibility=OwnerVisibility.PRIVATE)
        self.assertIn(self.pin, buyer.pins.all())

    def test_overlapping_previous_and_new_owner_names_is_rejected(self) -> None:
        response = self.client.post(
            reverse("pin.sales", args=[self.pin.slug]),
            {"previous_owners": "Same Co", "new_owners": "same co"},
        )
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

    def test_official_sale_cannot_be_deleted(self) -> None:
        sale = baker.make(PropertySale, location=self.location, source=OwnerSource.OFFICIAL)
        response = self.client.delete(reverse("pin.sales.delete", args=[self.pin.slug, sale.id]))
        self.assertEqual(response.status_code, 200)
        self.assertTrue(PropertySale.objects.filter(pk=sale.pk).exists())
