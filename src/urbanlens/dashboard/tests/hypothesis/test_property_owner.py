"""Tests for the Ownership card: private-pin-vs-shared-wiki separation (mirroring
PinAlias/WikiAlias), official-data locking, multi-owner sales, and permission gates."""

from __future__ import annotations

import datetime
from typing import TYPE_CHECKING

from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.models.property_owner.meta import OwnerSource
from urbanlens.dashboard.models.property_owner.model import PinOwner, PinPropertySale, WikiOwner, WikiPropertySale

if TYPE_CHECKING:
    from django.contrib.auth.models import User

    from urbanlens.dashboard.models.wiki.model import Wiki


class OwnerModelTests(TestCase):
    """Model-level behavior: shared multi-location association, sale ordering."""

    def test_wiki_owner_can_be_linked_to_multiple_locations(self) -> None:
        owner = baker.make(WikiOwner, name="Acme Holdings")
        location_a = baker.make(Location, latitude="41.0", longitude="-73.0")
        location_b = baker.make(Location, latitude="42.0", longitude="-74.0")
        owner.locations.add(location_a, location_b)
        self.assertCountEqual(WikiOwner.objects.for_location(location_a), [owner])
        self.assertCountEqual(WikiOwner.objects.for_location(location_b), [owner])

    def test_wiki_property_sale_queryset_orders_newest_first(self) -> None:
        location = baker.make(Location, latitude="41.0", longitude="-73.0")
        older = baker.make(WikiPropertySale, location=location, sale_date=datetime.date(2020, 1, 1))
        newer = baker.make(WikiPropertySale, location=location, sale_date=datetime.date(2024, 1, 1))
        self.assertEqual(list(WikiPropertySale.objects.for_location(location)), [newer, older])

    def test_pin_property_sale_queryset_orders_newest_first(self) -> None:
        pin = baker.make(Pin)
        older = baker.make(PinPropertySale, pin=pin, sale_date=datetime.date(2020, 1, 1))
        newer = baker.make(PinPropertySale, pin=pin, sale_date=datetime.date(2024, 1, 1))
        self.assertEqual(list(PinPropertySale.objects.for_pin(pin)), [newer, older])

    def test_deleting_wiki_owner_referenced_by_a_sale_removes_it_from_that_sale_only(self) -> None:
        location = baker.make(Location, latitude="41.0", longitude="-73.0")
        owner = baker.make(WikiOwner, name="Old Owner")
        sale = baker.make(WikiPropertySale, location=location)
        sale.previous_owners.add(owner)
        owner.delete()
        self.assertCountEqual(sale.previous_owners.all(), [])
        self.assertTrue(WikiPropertySale.objects.filter(pk=sale.pk).exists())


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


class PinOwnershipPanelViewTests(OwnershipPanelViewTestsBase):
    """GET/POST on the pin's private Ownership card."""

    def test_post_creates_a_private_owner_with_no_checkbox_needed(self) -> None:
        response = self.client.post(reverse("pin.ownership", args=[self.pin.slug]), {"name": "My Landlord"})
        self.assertEqual(response.status_code, 200)
        owner = PinOwner.objects.get(name="My Landlord")
        self.assertEqual(owner.pin_id, self.pin.pk)

    def test_pin_owner_is_not_visible_on_another_pin_at_the_same_location(self) -> None:
        self.client.post(reverse("pin.ownership", args=[self.pin.slug]), {"name": "My Landlord"})
        self.client.force_login(self.other_user)
        response = self.client.get(reverse("pin.ownership", args=[self.other_pin.slug]))
        self.assertNotContains(response, "My Landlord")

    def test_pin_owner_is_visible_on_the_pin_it_was_added_to(self) -> None:
        self.client.post(reverse("pin.ownership", args=[self.pin.slug]), {"name": "My Landlord"})
        response = self.client.get(reverse("pin.ownership", args=[self.pin.slug]))
        self.assertContains(response, "My Landlord")

    def test_wiki_owner_data_never_appears_on_the_pin_ownership_card(self) -> None:
        wiki_owner = baker.make(WikiOwner, name="Community Known Owner")
        wiki_owner.locations.add(self.location)
        response = self.client.get(reverse("pin.ownership", args=[self.pin.slug]))
        self.assertNotContains(response, "Community Known Owner")

    def test_post_without_a_name_is_rejected(self) -> None:
        response = self.client.post(reverse("pin.ownership", args=[self.pin.slug]), {"name": "  "})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(PinOwner.objects.exists())

    def test_viewing_a_pin_you_dont_own_404s(self) -> None:
        response = self.client.get(reverse("pin.ownership", args=[self.other_pin.slug]))
        self.assertEqual(response.status_code, 404)

    def test_updates_owner_fields(self) -> None:
        owner = baker.make(PinOwner, pin=self.pin, name="Old Name")
        response = self.client.post(reverse("pin.ownership.edit", args=[self.pin.slug, owner.id]), {"name": "New Name", "phone": "555-1234"})
        self.assertEqual(response.status_code, 200)
        owner.refresh_from_db()
        self.assertEqual(owner.name, "New Name")
        self.assertEqual(owner.phone, "555-1234")

    def test_editing_an_owner_belonging_to_another_pin_404s(self) -> None:
        owner = baker.make(PinOwner, pin=self.other_pin, name="Their Owner")
        response = self.client.post(reverse("pin.ownership.edit", args=[self.pin.slug, owner.id]), {"name": "Hijacked"})
        self.assertEqual(response.status_code, 404)

    def test_remove_deletes_the_pin_owner_record(self) -> None:
        owner = baker.make(PinOwner, pin=self.pin, name="Solo Owner")
        response = self.client.delete(reverse("pin.ownership.remove", args=[self.pin.slug, owner.id]))
        self.assertEqual(response.status_code, 200)
        self.assertFalse(PinOwner.objects.filter(pk=owner.pk).exists())

    def test_add_owner_form_is_hidden_until_the_header_button_is_clicked(self) -> None:
        """Regression guard: the owner-add form used to render un-hidden by
        default; it must stay collapsed to plain text/summary content until
        the section header's Add button reveals it."""
        response = self.client.get(reverse("pin.ownership", args=[self.pin.slug]))
        content = response.content.decode()
        self.assertIn('<form class="po-add-form" hidden', content)


class WikiOwnershipPanelViewTests(OwnershipPanelViewTestsBase):
    """GET/POST on the wiki's shared Ownership card."""

    def setUp(self) -> None:
        super().setUp()
        self.wiki: Wiki = baker.make("dashboard.Wiki", location=self.location, name="Curated Mill")

    def test_get_renders_shared_owners_for_the_location(self) -> None:
        owner = baker.make(WikiOwner, name="Diverset Corp")
        owner.locations.add(self.location)
        response = self.client.get(reverse("location.wiki.ownership", args=[self.location.slug]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Diverset Corp")

    def test_shared_owner_is_visible_on_a_different_profiles_pin_via_the_wiki(self) -> None:
        self.client.post(reverse("location.wiki.ownership", args=[self.location.slug]), {"name": "Community Owner"})
        self.client.force_login(self.other_user)
        response = self.client.get(reverse("location.wiki.ownership", args=[self.location.slug]))
        self.assertContains(response, "Community Owner")

    def test_pin_owner_data_never_appears_on_the_wiki_ownership_card(self) -> None:
        baker.make(PinOwner, pin=self.pin, name="My Private Note")
        response = self.client.get(reverse("location.wiki.ownership", args=[self.location.slug]))
        self.assertNotContains(response, "My Private Note")

    def test_viewing_the_wiki_without_a_pin_here_404s(self) -> None:
        third_user: User = baker.make("auth.User")
        self.client.force_login(third_user)
        response = self.client.get(reverse("location.wiki.ownership", args=[self.location.slug]))
        self.assertEqual(response.status_code, 404)

    def test_unlinks_shared_owner_but_keeps_the_record_if_linked_elsewhere(self) -> None:
        other_location = baker.make(Location, latitude="10.0", longitude="10.0")
        owner = baker.make(WikiOwner, name="Multi Property Owner")
        owner.locations.add(self.location, other_location)
        response = self.client.delete(reverse("location.wiki.ownership.remove", args=[self.location.slug, owner.id]))
        self.assertEqual(response.status_code, 200)
        owner.refresh_from_db()
        self.assertNotIn(self.location, owner.locations.all())
        self.assertIn(other_location, owner.locations.all())

    def test_official_owner_cannot_be_edited(self) -> None:
        owner = baker.make(WikiOwner, name="Official Records Inc", source=OwnerSource.OFFICIAL)
        owner.locations.add(self.location)
        response = self.client.post(reverse("location.wiki.ownership.edit", args=[self.location.slug, owner.id]), {"name": "Tampered Name"})
        self.assertEqual(response.status_code, 200)
        owner.refresh_from_db()
        self.assertEqual(owner.name, "Official Records Inc")

    def test_official_owner_cannot_be_removed(self) -> None:
        owner = baker.make(WikiOwner, name="Official Records Inc", source=OwnerSource.OFFICIAL)
        owner.locations.add(self.location)
        response = self.client.delete(reverse("location.wiki.ownership.remove", args=[self.location.slug, owner.id]))
        self.assertEqual(response.status_code, 200)
        owner.refresh_from_db()
        self.assertIn(self.location, owner.locations.all())


class PinPropertySaleTabViewTests(OwnershipPanelViewTestsBase):
    """GET/POST on the pin's private Sale History tab."""

    def test_sale_is_not_visible_on_another_pin_at_the_same_location(self) -> None:
        self.client.post(reverse("pin.sales", args=[self.pin.slug]), {"notes": "My private deal"})
        self.client.force_login(self.other_user)
        response = self.client.get(reverse("pin.sales", args=[self.other_pin.slug]))
        self.assertNotContains(response, "My private deal")

    def test_post_creates_a_sale_with_multiple_previous_and_new_owners(self) -> None:
        response = self.client.post(
            reverse("pin.sales", args=[self.pin.slug]),
            {"previous_owners": "Seller One, Seller Two", "new_owners": "Buyer One, Buyer Two, Buyer Three", "sale_price": "300000", "sale_date": "2024-06-01"},
        )
        self.assertEqual(response.status_code, 200)
        sale = PinPropertySale.objects.get(pin=self.pin)
        self.assertCountEqual(sale.previous_owners.values_list("name", flat=True), ["Seller One", "Seller Two"])
        self.assertCountEqual(sale.new_owners.values_list("name", flat=True), ["Buyer One", "Buyer Two", "Buyer Three"])
        self.assertEqual(str(sale.sale_price), "300000.00")

    def test_overlapping_previous_and_new_owner_names_is_rejected(self) -> None:
        response = self.client.post(reverse("pin.sales", args=[self.pin.slug]), {"previous_owners": "Same Co", "new_owners": "same co"})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(PinPropertySale.objects.exists())

    def test_invalid_sale_price_is_rejected(self) -> None:
        response = self.client.post(reverse("pin.sales", args=[self.pin.slug]), {"sale_price": "not-a-number"})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(PinPropertySale.objects.exists())

    def test_negative_sale_price_is_rejected(self) -> None:
        response = self.client.post(reverse("pin.sales", args=[self.pin.slug]), {"sale_price": "-100"})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(PinPropertySale.objects.exists())

    def test_invalid_sale_date_is_rejected(self) -> None:
        response = self.client.post(reverse("pin.sales", args=[self.pin.slug]), {"sale_date": "not-a-date"})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(PinPropertySale.objects.exists())

    def test_deletes_the_sale(self) -> None:
        sale = baker.make(PinPropertySale, pin=self.pin)
        response = self.client.delete(reverse("pin.sales.delete", args=[self.pin.slug, sale.id]))
        self.assertEqual(response.status_code, 200)
        self.assertFalse(PinPropertySale.objects.filter(pk=sale.pk).exists())

    def test_deleting_a_sale_for_another_pin_404s(self) -> None:
        sale = baker.make(PinPropertySale, pin=self.other_pin)
        response = self.client.delete(reverse("pin.sales.delete", args=[self.pin.slug, sale.id]))
        self.assertEqual(response.status_code, 404)

    def test_add_sale_form_is_hidden_until_the_header_button_is_clicked(self) -> None:
        """Regression guard: unlike the owner-add form, the sale-add form used
        to render with no `hidden` attribute at all - a page of empty inputs
        was shown before the user ever clicked "Add"."""
        response = self.client.get(reverse("pin.sales", args=[self.pin.slug]))
        content = response.content.decode()
        self.assertIn('class="po-add-form po-sale-add-form" hidden', content)

    def test_add_sale_form_stays_hidden_after_recording_a_sale(self) -> None:
        response = self.client.post(reverse("pin.sales", args=[self.pin.slug]), {"sale_price": "1000"})
        content = response.content.decode()
        self.assertIn('class="po-add-form po-sale-add-form" hidden', content)


class WikiPropertySaleTabViewTests(OwnershipPanelViewTestsBase):
    """GET/POST on the wiki's shared Sale History tab."""

    def setUp(self) -> None:
        super().setUp()
        self.wiki: Wiki = baker.make("dashboard.Wiki", location=self.location, name="Curated Mill")

    def test_recording_a_sale_removes_previous_owners_from_current_shared_owners(self) -> None:
        seller = baker.make(WikiOwner, name="Seller Co")
        seller.locations.add(self.location)
        self.client.post(reverse("location.wiki.sales", args=[self.location.slug]), {"previous_owners": "Seller Co", "new_owners": "Buyer Co"})
        seller.refresh_from_db()
        self.assertNotIn(self.location, seller.locations.all())
        buyer = WikiOwner.objects.get(name="Buyer Co")
        self.assertIn(self.location, buyer.locations.all())

    def test_official_sale_cannot_be_deleted(self) -> None:
        sale = baker.make(WikiPropertySale, location=self.location, source=OwnerSource.OFFICIAL)
        response = self.client.delete(reverse("location.wiki.sales.delete", args=[self.location.slug, sale.id]))
        self.assertEqual(response.status_code, 200)
        self.assertTrue(WikiPropertySale.objects.filter(pk=sale.pk).exists())

    def test_sale_not_visible_without_a_pin_at_the_location(self) -> None:
        self.client.post(reverse("location.wiki.sales", args=[self.location.slug]), {"notes": "Community sale"})
        third_user: User = baker.make("auth.User")
        self.client.force_login(third_user)
        response = self.client.get(reverse("location.wiki.sales", args=[self.location.slug]))
        self.assertEqual(response.status_code, 404)
