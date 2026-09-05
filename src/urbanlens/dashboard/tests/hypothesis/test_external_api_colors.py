"""A colour the external API cannot store is refused, not quietly replaced.

`clean_color` substitutes the default for anything it does not recognise. That
is right for a form post, where the user sees the swatch that resulted, and
wrong for an API: the client is told 200, the value it sent is gone, and the
only way to find out is to read the record back and compare.

The endpoints did not agree about this. The single-label and saved-filter
writes were already strict, through a serializer restricted to the shared
`COLOR_CHOICES` palette. Pin create, the label bulk edit and the label
customization override all took any string and dropped what they could not use.
These cover both kinds, so the strict ones stay strict and the rest cannot go
back.

Two ways to send a colour that is not stored, and the second is the surprising
one: `red` is not hex at all, and `#f00` is - it is just the 3-digit form,
which this application does not accept.

Missing and blank are not covered by any of this: they mean "leave it alone" or
"clear it" at every one of these endpoints, and must keep doing so.
"""

from __future__ import annotations

from django.contrib.auth.models import User
from model_bakery import baker

from urbanlens.core.tests.labels import ensure_label
from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.account.model import ApiKey, ApiKeyScope
from urbanlens.dashboard.models.labels.customization import LabelCustomization
from urbanlens.dashboard.models.labels.meta import COLOR_CHOICES, KIND_TAG
from urbanlens.dashboard.models.labels.model import Label
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.models.saved_filter.model import SavedFilter
from urbanlens.dashboard.services.auth.api_keys import generate_api_key

_LABELS = "/dashboard/api/external/v1/labels/"
_FILTERS = "/dashboard/api/external/v1/saved-filters/"
_PINS = "/dashboard/api/external/v1/pins/"

#: Escapes the enclosing style attribute, and fits `Pin.color`'s varchar(20) -
#: the longer `" onmouseover="alert(1)` does not, so a length cap alone would
#: look like a working guard.
BREAKOUT = '" onclick=alert(1)'

#: Rejected for three different reasons, so a fix handling only one shows up.
NOT_COLOURS = ("red", "#f00", BREAKOUT)

#: A colour from the shared palette, for the positive cases - the label and
#: saved-filter endpoints accept nothing else.
PALETTE_COLOR = COLOR_CHOICES[0][0]


class ColorApiTestCase(TestCase):
    """A key carrying every scope these endpoints need."""

    def setUp(self) -> None:
        super().setUp()
        self.user = baker.make(User)
        self.profile = Profile.objects.get(user=self.user)
        _key, self.raw_key = generate_api_key(self.user, "Colours client")
        ApiKey.objects.filter(user=self.user).update(
            scopes=[
                ApiKeyScope.LABELS_READ.value,
                ApiKeyScope.LABELS_WRITE.value,
                ApiKeyScope.LISTS_READ.value,
                ApiKeyScope.LISTS_WRITE.value,
                ApiKeyScope.PINS_READ.value,
                ApiKeyScope.PINS_WRITE.value,
            ],
        )

    @property
    def _auth(self) -> dict:
        return {"HTTP_AUTHORIZATION": f"Bearer {self.raw_key}"}

    def _post(self, url: str, body: dict):
        return self.client.post(url, body, content_type="application/json", **self._auth)

    def _patch(self, url: str, body: dict):
        return self.client.patch(url, body, content_type="application/json", **self._auth)

    def _put(self, url: str, body: dict):
        return self.client.put(url, body, content_type="application/json", **self._auth)


class PinColorRejectionTests(ColorApiTestCase):
    """POST ``pins/`` - a plain CharField, so nothing rejected before this."""

    def _body(self, **extra) -> dict:
        return {"name": "Cx pin", "latitude": 41.0, "longitude": -73.0, **extra}

    def test_a_non_colour_is_refused(self) -> None:
        for value in NOT_COLOURS:
            with self.subTest(color=value):
                response = self._post(_PINS, self._body(color=value))
                self.assertEqual(response.status_code, 400, response.content)
                self.assertIn("color", response.json().get("fields", {}))

    def test_a_refused_create_stores_no_pin(self) -> None:
        self._post(_PINS, self._body(name="CxGhostPin", color="red"))
        self.assertFalse(Pin.objects.filter(name="CxGhostPin").exists())

    def test_a_real_colour_is_still_accepted(self) -> None:
        response = self._post(_PINS, self._body(name="CxGoodPin", color="#0a1b2c"))
        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(Pin.objects.get(name="CxGoodPin").color, "#0a1b2c")

    def test_an_omitted_colour_still_creates(self) -> None:
        self.assertEqual(self._post(_PINS, self._body(name="CxNoColour")).status_code, 201)

    def test_a_blank_colour_still_creates(self) -> None:
        self.assertEqual(self._post(_PINS, self._body(name="CxBlank", color="")).status_code, 201)


class PinColorUpdateRejectionTests(ColorApiTestCase):
    """PATCH ``pins/{slug}/`` - the sibling POST hardened, and this did not.

    `Pin.color` is served as `effective_color` and interpolated into a Leaflet
    `divIcon`'s `html`, so a string that is not a colour is a stored injection
    into the owner's own map, not a cosmetic problem.
    """

    def _pin(self):
        created = self._post(_PINS, {"name": "Cx patch pin", "latitude": 41.0, "longitude": -73.0})
        self.assertEqual(created.status_code, 201, created.content)
        return Pin.objects.get(uuid=created.json()["uuid"])

    def test_a_breakout_payload_is_refused(self) -> None:
        pin = self._pin()
        response = self._patch(f"{_PINS}{pin.slug}/", {"color": BREAKOUT})
        self.assertEqual(response.status_code, 400, response.content)
        pin.refresh_from_db()
        self.assertIsNone(pin.color)

    def test_a_non_colour_is_refused(self) -> None:
        pin = self._pin()
        for value in NOT_COLOURS:
            with self.subTest(color=value):
                response = self._patch(f"{_PINS}{pin.slug}/", {"color": value})
                self.assertEqual(response.status_code, 400, response.content)

    def test_a_real_colour_still_applies(self) -> None:
        pin = self._pin()
        response = self._patch(f"{_PINS}{pin.slug}/", {"color": "#0a1b2c"})
        self.assertEqual(response.status_code, 200, response.content)
        pin.refresh_from_db()
        self.assertEqual(pin.color, "#0a1b2c")

    def test_a_blank_colour_still_clears(self) -> None:
        pin = self._pin()
        Pin.objects.filter(pk=pin.pk).update(color="#0a1b2c")
        response = self._patch(f"{_PINS}{pin.slug}/", {"color": ""})
        self.assertEqual(response.status_code, 200, response.content)
        pin.refresh_from_db()
        self.assertIsNone(pin.color)


class PinModelColorCoercionTests(ColorApiTestCase):
    """The column itself, for the write paths no serializer stands in front of.

    The floorplan editor's save assigns `linked.color` straight from its JSON
    body, and the archive importer assigns three pin colour columns from an
    uploaded file. Neither goes near the external API's validation.
    """

    def test_save_drops_a_value_that_is_not_a_colour(self) -> None:
        pin = self._pin_direct(color=BREAKOUT)
        self.assertIsNone(pin.color)
        self.assertIsNone(Pin.objects.get(pk=pin.pk).color)

    def test_save_keeps_a_real_colour(self) -> None:
        self.assertEqual(self._pin_direct(color="#0a1b2c").color, "#0a1b2c")

    def test_an_update_after_creation_is_coerced_too(self) -> None:
        pin = self._pin_direct(color="#0a1b2c")
        pin.color = "rgb(1,2,3)"
        pin.save()
        pin.refresh_from_db()
        self.assertIsNone(pin.color)

    def _pin_direct(self, **kwargs):
        from urbanlens.dashboard.models.location.model import Location

        location = baker.make(Location, latitude="41.0", longitude="-73.0")
        return Pin.objects.create(profile=self.profile, name="Cx direct", location=location, **kwargs)


class SavedFilterModelColorCoercionTests(ColorApiTestCase):
    """`SavedFilter.color` is tinted into an inline style the same way."""

    def test_save_drops_a_value_that_is_not_a_colour(self) -> None:
        row = SavedFilter.objects.create(profile=self.profile, name="CxRaw", criteria={}, color=BREAKOUT)
        self.assertEqual(SavedFilter.objects.get(pk=row.pk).color, "")

    def test_save_keeps_a_palette_colour(self) -> None:
        row = SavedFilter.objects.create(profile=self.profile, name="CxRawOk", criteria={}, color=PALETTE_COLOR)
        self.assertEqual(row.color, PALETTE_COLOR)


class LabelColorRejectionTests(ColorApiTestCase):
    """POST/PATCH ``labels/`` - already strict, and must stay so."""

    def test_creating_a_label_with_a_non_colour_is_refused(self) -> None:
        for value in NOT_COLOURS:
            with self.subTest(color=value):
                response = self._post(_LABELS, {"name": f"Cx{value[:3]}", "kind": KIND_TAG, "color": value})
                self.assertEqual(response.status_code, 400, response.content)
                self.assertIn("color", response.json().get("fields", {}))

    def test_a_hex_outside_the_palette_is_refused_too(self) -> None:
        """The label column declares `choices`; the serializer enforces them."""
        response = self._post(_LABELS, {"name": "CxOffPalette", "kind": KIND_TAG, "color": "#0a1b2c"})
        self.assertEqual(response.status_code, 400, response.content)

    def test_a_refused_create_stores_nothing(self) -> None:
        self._post(_LABELS, {"name": "CxGhost", "kind": KIND_TAG, "color": "red"})
        self.assertFalse(Label.objects.filter(name="CxGhost").exists())

    def test_patching_a_label_with_a_non_colour_is_refused(self) -> None:
        label = ensure_label(profile=self.profile, name="CxPatch", kind=KIND_TAG, color=PALETTE_COLOR)
        response = self._patch(f"{_LABELS}{label.uuid}/", {"color": "red"})
        self.assertEqual(response.status_code, 400, response.content)
        label.refresh_from_db()
        self.assertEqual(label.color, PALETTE_COLOR, "the stored colour must survive a refused write")

    def test_a_palette_colour_is_still_accepted(self) -> None:
        response = self._post(_LABELS, {"name": "CxGood", "kind": KIND_TAG, "color": PALETTE_COLOR})
        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(Label.objects.get(name="CxGood").color, PALETTE_COLOR)

    def test_a_blank_colour_still_clears_rather_than_failing(self) -> None:
        label = ensure_label(profile=self.profile, name="CxClear", kind=KIND_TAG, color=PALETTE_COLOR)
        response = self._patch(f"{_LABELS}{label.uuid}/", {"color": ""})
        self.assertEqual(response.status_code, 200, response.content)
        label.refresh_from_db()
        self.assertIsNone(label.color)


class LabelBulkEditColorRejectionTests(ColorApiTestCase):
    """``labels/bulk/edit/`` writes with `bulk_update`, which skips `save()`."""

    def test_a_non_colour_is_refused_before_the_bulk_write(self) -> None:
        label = ensure_label(profile=self.profile, name="CxBulk", kind=KIND_TAG, color=PALETTE_COLOR)
        response = self._post(f"{_LABELS}bulk/edit/", {"uuids": [str(label.uuid)], "color": "red"})
        self.assertEqual(response.status_code, 400, response.content)
        label.refresh_from_db()
        self.assertEqual(label.color, PALETTE_COLOR)

    def test_it_enforces_the_same_palette_as_the_single_label_endpoint(self) -> None:
        """The two write the same column; they used to disagree about what fits."""
        label = ensure_label(profile=self.profile, name="CxBulkOff", kind=KIND_TAG, color=PALETTE_COLOR)
        response = self._post(f"{_LABELS}bulk/edit/", {"uuids": [str(label.uuid)], "color": "#0a1b2c"})
        self.assertEqual(response.status_code, 400, response.content)

    def test_a_palette_colour_still_applies(self) -> None:
        label = ensure_label(profile=self.profile, name="CxBulkOk", kind=KIND_TAG, color=PALETTE_COLOR)
        other = COLOR_CHOICES[1][0]
        response = self._post(f"{_LABELS}bulk/edit/", {"uuids": [str(label.uuid)], "color": other})
        self.assertEqual(response.status_code, 200, response.content)
        label.refresh_from_db()
        self.assertEqual(label.color, other)

    def test_the_queryset_coerces_a_bulk_update_from_anywhere_else(self) -> None:
        """The endpoint validates; every other caller of `bulk_update` does not."""
        label = ensure_label(profile=self.profile, name="CxBulkRaw", kind=KIND_TAG, color=PALETTE_COLOR)
        label.color = "red"
        Label.objects.bulk_update([label], ["color"])
        label.refresh_from_db()
        self.assertIsNone(label.color)


class LabelCustomizationColorRejectionTests(ColorApiTestCase):
    """PUT ``labels/{uuid}/customization/`` - the override that actually renders."""

    def test_a_non_colour_is_refused(self) -> None:
        label = ensure_label(profile=None, name="CxSharedLabel", kind=KIND_TAG)
        response = self._put(f"{_LABELS}{label.uuid}/customization/", {"color": "red"})
        self.assertEqual(response.status_code, 400, response.content)
        self.assertFalse(LabelCustomization.objects.filter(profile=self.profile, label=label).exists())

    def test_a_real_colour_is_accepted(self) -> None:
        label = ensure_label(profile=None, name="CxSharedOk", kind=KIND_TAG)
        response = self._put(f"{_LABELS}{label.uuid}/customization/", {"color": "#0a1b2c"})
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(LabelCustomization.objects.get(profile=self.profile, label=label).color, "#0a1b2c")


class SavedFilterColorRejectionTests(ColorApiTestCase):
    """POST/PATCH ``saved-filters/`` - already strict, and must stay so."""

    def _create(self, **extra):
        return self._post(_FILTERS, {"name": "CxFilterOk", "criteria": {}, **extra})

    def test_creating_with_a_non_colour_is_refused(self) -> None:
        response = self._post(_FILTERS, {"name": "CxFilter", "criteria": {}, "color": "red"})
        self.assertEqual(response.status_code, 400, response.content)
        self.assertFalse(SavedFilter.objects.filter(name="CxFilter").exists())

    def test_patching_with_a_non_colour_is_refused(self) -> None:
        created = self._create(color=PALETTE_COLOR)
        self.assertEqual(created.status_code, 201, created.content)
        uuid = created.json()["uuid"]

        response = self._patch(f"{_FILTERS}{uuid}/", {"color": "#f00"})
        self.assertEqual(response.status_code, 400, response.content)
        self.assertEqual(SavedFilter.objects.get(uuid=uuid).color, PALETTE_COLOR)
