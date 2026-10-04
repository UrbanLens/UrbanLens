"""P16: with "child pin details" on, a parent pin's page lists its child pins' aliases and labels, read-only.

Editing stays on each child's own page, the way the parent already shows its children's notes.
"""

from __future__ import annotations

from django.contrib.auth.models import User
from django.test import RequestFactory
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.aliases.model import PinAlias
from urbanlens.dashboard.models.labels.model import Label
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin, PinType
from urbanlens.dashboard.services.core.child_details import child_details_requested


def _location(offset: float) -> Location:
    return baker.make(Location, latitude=f"{41.7 + offset:.6f}", longitude="-73.900000")


class _ParentWithChild(TestCase):
    def setUp(self) -> None:
        super().setUp()
        baker.make(User)
        self.user = baker.make(User)
        self.profile = self.user.profile
        self.parent = baker.make(
            Pin,
            profile=self.profile,
            location=_location(0),
            name="Campus",
            pin_type=PinType.PARCEL,
            pin_type_is_user_provided=True,
        )
        self.child = baker.make(
            Pin, profile=self.profile, location=_location(0.001), name="Powerhouse", parent_pin=self.parent
        )
        self.client.force_login(self.user)


class ChildAliasListingTests(_ParentWithChild):
    def setUp(self) -> None:
        super().setUp()
        self.child_alias = PinAlias.objects.create(pin=self.child, name="Building 33")

    def _panel(self, children: str, **headers: str):
        return self.client.get(reverse("pin.aliases", args=[self.parent.slug]), {"children": children}, **headers)

    def test_the_parent_lists_a_childs_alias_beside_a_link_to_the_child(self) -> None:
        response = self._panel("1")

        self.assertContains(response, "Building 33")
        self.assertContains(response, reverse("pin.details", kwargs={"pin_slug": self.child.slug}))
        self.assertContains(response, 'class="child-chip"')

    def test_an_alias_that_repeats_the_childs_name_is_not_listed_again(self) -> None:
        """A pin keeps its own name as an alias, and the chip already names the child."""
        lonely = baker.make(
            Pin, profile=self.profile, location=_location(0.002), name="Tool shed", parent_pin=self.parent
        )
        for pin, name in ((self.child, "Powerhouse"), (lonely, "Tool shed")):
            PinAlias.objects.get_or_create(pin=pin, name__iexact=name, defaults={"name": name.upper()})

        response = self._panel("1")

        self.assertEqual(response.content.decode().count('class="child-chip"'), 1)
        self.assertNotContains(response, "Tool shed")
        self.assertNotContains(response, '<span class="alias-chip-name">Powerhouse</span>')
        self.assertNotContains(response, '<span class="alias-chip-name">POWERHOUSE</span>')

    def test_a_childs_alias_cannot_be_edited_from_the_parent(self) -> None:
        response = self._panel("1")

        self.assertNotContains(response, reverse("pin.alias.delete", args=[self.parent.slug, self.child_alias.pk]))
        self.assertNotContains(response, reverse("pin.alias.use", args=[self.parent.slug, self.child_alias.pk]))

    def test_with_child_details_off_the_childs_alias_is_not_listed(self) -> None:
        self.assertNotContains(self._panel("0"), "Building 33")

    def test_adding_an_alias_keeps_the_childs_listed_while_the_page_shows_them(self) -> None:
        page = reverse("pin.details", kwargs={"pin_slug": self.parent.slug}) + "?children=1"

        response = self.client.post(
            reverse("pin.aliases", args=[self.parent.slug]),
            {"name": "The old asylum"},
            HTTP_HX_REQUEST="true",
            HTTP_HX_CURRENT_URL=f"http://testserver{page}",
        )

        self.assertContains(response, "The old asylum")
        self.assertContains(response, "Building 33")


class ChildLabelListingTests(_ParentWithChild):
    def setUp(self) -> None:
        super().setUp()
        self.label = baker.make(Label, profile=self.profile, name="Asbestos", kind="tag")
        self.child.labels.add(self.label)

    def _panel(self, children: str):
        return self.client.get(
            reverse("label.pin", kwargs={"label_kind": "category", "pin_slug": self.parent.slug}),
            {"children": children},
        )

    def test_the_parent_lists_a_childs_label(self) -> None:
        response = self._panel("1")

        self.assertContains(response, 'class="child-chip"')
        self.assertContains(response, reverse("pin.details", kwargs={"pin_slug": self.child.slug}))
        self.assertContains(response, "Asbestos")

    def test_the_parents_own_labels_are_unchanged_by_its_childs(self) -> None:
        response = self._panel("1")

        self.assertNotContains(
            response, f'name="label_id" value="{self.label.pk}"><input type="hidden" name="action" value="remove"'
        )
        self.assertFalse(self.parent.labels.filter(pk=self.label.pk).exists())

    def test_with_child_details_off_the_childs_label_is_not_listed(self) -> None:
        self.assertNotContains(self._panel("0"), 'class="child-chip"')


class ToggleRegionsTests(_ParentWithChild):
    def test_turning_child_details_on_reloads_the_alias_and_label_panels_with_it(self) -> None:
        response = self.client.get(reverse("pin.child_details", args=[self.parent.slug]), {"children": "1"})

        content = response.content.decode()
        self.assertIn(reverse("pin.aliases", args=[self.parent.slug]) + "?children=1", content)
        self.assertIn(
            reverse("label.pin", kwargs={"label_kind": "category", "pin_slug": self.parent.slug}) + "?children=1",
            content,
        )


class ChildDetailsRequestedTests(_ParentWithChild):
    def _request(self, query: str = "", current_url: str | None = None):
        headers = {"HTTP_HX_CURRENT_URL": current_url} if current_url else {}
        return RequestFactory().get(f"/x/{query}", **headers)

    def _page(self, query: str = "") -> str:
        return "http://testserver" + reverse("pin.details", kwargs={"pin_slug": self.parent.slug}) + query

    def test_the_requests_own_flag_decides_first(self) -> None:
        request = self._request("?children=0", current_url=self._page("?children=1"))

        self.assertFalse(child_details_requested(request, self.parent))

    def test_the_page_url_decides_when_the_request_carries_no_flag(self) -> None:
        """The parcel's default is on, so only the page's own flag can turn it off here."""
        self.assertFalse(child_details_requested(self._request(current_url=self._page("?children=0")), self.parent))

    def test_otherwise_the_pins_default_decides(self) -> None:
        """A parcel's children are its content, so its page starts with them shown."""
        self.assertTrue(child_details_requested(self._request(current_url=self._page()), self.parent))

    def test_a_panel_opened_from_another_page_lists_the_pin_alone(self) -> None:
        """The map's label dialog posts with the map as its page; the parcel's default is its own page's."""
        self.assertFalse(child_details_requested(self._request(current_url="http://testserver/map/"), self.parent))
        self.assertFalse(child_details_requested(self._request(), self.parent))

    def test_another_pins_page_flag_does_not_carry_over(self) -> None:
        child_page = "http://testserver" + reverse("pin.details", kwargs={"pin_slug": self.child.slug}) + "?children=1"

        self.assertFalse(child_details_requested(self._request(current_url=child_page), self.parent))
