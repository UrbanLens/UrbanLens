"""Tests that label colours cannot be stored as arbitrary strings.

`Label.color` carries `choices`, which is a form-layer constraint and not a
database one: `services/import_export/import_data.py` builds labels straight
from an uploaded file's rows, and the external API assigns from a JSON body.
The value is then interpolated into `style="..."` attributes across the label
chip, merge-form and organize templates, so whatever reaches the column renders.

`LabelCustomization.color` is the weaker of the two - no `choices` at all - and
it *wins* over the label's own value in `Label.effective_color`, so it is the
one that actually renders wherever a user has set an override.

These pin the coercion at the model, which is what covers every write path at
once, plus the `bulk_create` route that does not call `save()` and so needs its
own.
"""

from __future__ import annotations

from model_bakery import baker

from hypothesis import given, settings, strategies as st
from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.labels.customization.model import LabelCustomization
from urbanlens.dashboard.models.labels.meta import KIND_TAG
from urbanlens.dashboard.models.labels.model import Label

#: A payload that escapes the enclosing style attribute if written through unescaped.
BREAKOUT = '" onmouseover="alert(1)'

#: Values a colour column must never end up holding.
NOT_COLOURS = [
    BREAKOUT,
    "red",
    "rgb(1,2,3)",
    "url(javascript:alert(1))",
    "#f00",
    "#12345g",
    "  ",
    "expression(alert(1))",
]


class LabelColorStorageTests(TestCase):
    """`Label.save()` coerces the colour whichever path set it."""

    def setUp(self):
        super().setUp()
        self.profile = baker.make("auth.User").profile
        self.counter = 0

    def _make(self, **kwargs) -> Label:
        self.counter += 1
        return Label.objects.create(profile=self.profile, name=f"label-{self.counter}", kind=KIND_TAG, **kwargs)

    def test_a_valid_colour_is_kept(self):
        self.assertEqual(self._make(color="#1a2b3c").color, "#1a2b3c")

    def test_uppercase_hex_digits_are_kept(self):
        self.assertEqual(self._make(color="#ABCDEF").color, "#ABCDEF")

    def test_an_unset_colour_stays_null(self):
        self.assertIsNone(self._make().color)

    def test_an_attribute_breakout_is_dropped_on_create(self):
        self.assertIsNone(self._make(color=BREAKOUT).color)

    def test_an_attribute_breakout_is_dropped_on_update(self):
        label = self._make(color="#1a2b3c")
        label.color = BREAKOUT
        label.save()
        label.refresh_from_db()
        self.assertIsNone(label.color)

    def test_the_coercion_is_what_the_database_returns(self):
        """Persisted, not merely applied to the in-memory object."""
        label = self._make(color=BREAKOUT)
        self.assertIsNone(Label.objects.get(pk=label.pk).color)

    def test_bulk_create_coerces_too(self):
        """`bulk_create` never calls `save()`, so it needs its own pass."""
        Label.objects.bulk_create(
            [
                Label(profile=self.profile, name="bulk-good", kind=KIND_TAG, color="#123456"),
                Label(profile=self.profile, name="bulk-bad", kind=KIND_TAG, color=BREAKOUT),
            ],
        )
        self.assertEqual(Label.objects.get(name="bulk-good").color, "#123456")
        self.assertIsNone(Label.objects.get(name="bulk-bad").color)

    def test_an_imported_row_cannot_smuggle_a_colour_past_choices(self):
        """What `choices` does not cover: a value that never met a form."""
        for value in NOT_COLOURS:
            with self.subTest(value=value):
                self.assertIsNone(self._make(color=value).color)


class LabelCustomizationColorStorageTests(TestCase):
    """The per-user override is coerced too, and it is the one that renders."""

    def setUp(self):
        super().setUp()
        self.profile = baker.make("auth.User").profile
        self.label = Label.objects.create(name="global", kind=KIND_TAG, color="#123456")

    def test_a_valid_override_is_kept(self):
        row = LabelCustomization.objects.create(profile=self.profile, label=self.label, color="#abcdef")
        self.assertEqual(row.color, "#abcdef")

    def test_an_attribute_breakout_is_dropped(self):
        row = LabelCustomization.objects.create(profile=self.profile, label=self.label, color=BREAKOUT)
        self.assertIsNone(LabelCustomization.objects.get(pk=row.pk).color)

    def test_bulk_create_coerces_too(self):
        LabelCustomization.objects.bulk_create(
            [LabelCustomization(profile=self.profile, label=self.label, color=BREAKOUT)],
        )
        self.assertIsNone(LabelCustomization.objects.get(label=self.label).color)

    def test_a_dropped_override_falls_back_to_the_label(self):
        """`effective_color` reads the override first, so dropping must not blank it."""
        LabelCustomization.objects.create(profile=self.profile, label=self.label, color=BREAKOUT)
        self.label._user_customizations = list(LabelCustomization.objects.filter(label=self.label))
        self.assertEqual(self.label.effective_color, "#123456")


class LabelColorPropertyTests(TestCase):
    """Whatever is written, what comes back is a storable colour or nothing."""

    def setUp(self):
        super().setUp()
        self.profile = baker.make("auth.User").profile

    @given(st.text(max_size=50))
    @settings(max_examples=50, deadline=None)
    def test_a_stored_colour_is_always_six_digit_hex_or_null(self, value: str) -> None:
        label = Label(profile=self.profile, name="probe", kind=KIND_TAG, color=value)
        label.coerce_colors()
        self.assertTrue(label.color is None or (len(label.color) == 7 and label.color.startswith("#")), label.color)
