"""Tests that PinMarkup colours cannot be stored as arbitrary strings.

``color`` and ``border_color`` are interpolated into markup that reaches
``innerHTML`` on the client (text-label spans, arrowhead SVG), and every write
path builds the model directly from a JSON body rather than through a Form, so
the restriction is enforced in ``PinMarkup.save()``. These tests pin that down
at the model - which covers the create/edit endpoints, snapshot imports, and
map clones alike - plus the pure helpers the renderers share.

Model tests require the database; the helper tests do not.
"""

from __future__ import annotations

from django.test import SimpleTestCase
from hypothesis import given, strategies as st
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.markup.model import MarkupType, PinMarkup
from urbanlens.dashboard.services.core.colors import sanitize_hex_color, sanitize_optional_color

# A payload that escapes the enclosing attribute if written through unescaped.
BREAKOUT = '" onmouseover="alert(1)'


class SanitizeHexColorTests(SimpleTestCase):
    """sanitize_hex_color keeps real colours and rejects everything else."""

    def test_passes_through_six_digit_hex(self):
        self.assertEqual(sanitize_hex_color("#1a2b3c"), "#1a2b3c")

    def test_accepts_uppercase_hex_digits(self):
        self.assertEqual(sanitize_hex_color("#ABCDEF"), "#ABCDEF")

    def test_rejects_attribute_breakout(self):
        self.assertEqual(sanitize_hex_color(BREAKOUT), "#e74c3c")

    def test_rejects_named_and_functional_colours(self):
        self.assertEqual(sanitize_hex_color("red"), "#e74c3c")
        self.assertEqual(sanitize_hex_color("rgb(1,2,3)"), "#e74c3c")

    def test_rejects_short_and_long_hex(self):
        self.assertEqual(sanitize_hex_color("#abc"), "#e74c3c")
        self.assertEqual(sanitize_hex_color("#aabbccdd"), "#e74c3c")

    def test_rejects_non_strings(self):
        self.assertEqual(sanitize_hex_color(None), "#e74c3c")
        self.assertEqual(sanitize_hex_color(123456), "#e74c3c")

    def test_uses_the_given_fallback(self):
        self.assertEqual(sanitize_hex_color("nope", "#000000"), "#000000")

    @given(st.text())
    def test_output_is_always_a_hex_colour(self, value: str):
        """Whatever goes in, what comes out is safe to interpolate."""
        result = sanitize_hex_color(value)
        self.assertRegex(result, r"^#[0-9a-fA-F]{6}$")


class SanitizeOptionalColorTests(SimpleTestCase):
    """sanitize_optional_color additionally allows the "none" sentinel."""

    def test_keeps_the_none_sentinel(self):
        self.assertEqual(sanitize_optional_color("none"), "none")

    def test_keeps_a_real_colour(self):
        self.assertEqual(sanitize_optional_color("#112233"), "#112233")

    def test_falls_back_to_unset_for_junk(self):
        self.assertEqual(sanitize_optional_color(BREAKOUT), "")
        self.assertEqual(sanitize_optional_color("chartreuse"), "")

    @given(st.text())
    def test_output_is_always_hex_none_or_empty(self, value: str):
        result = sanitize_optional_color(value)
        self.assertTrue(result == "" or result == "none" or len(result) == 7, result)


class PinMarkupBulkCreateColorTests(TestCase):
    """bulk_create never calls save(), so the coercion has to reach it separately.

    The undo restore rebuilds a deleted map's annotations this way, from a
    payload captured at delete time - which is precisely where a value stored
    before this validation existed would still be sitting.
    """

    def setUp(self):
        self.user = baker.make("auth.User")
        self.profile = self.user.profile
        self.pin = baker.make("dashboard.Pin", profile=self.profile)

    def _unsaved(self, **kwargs) -> PinMarkup:
        return PinMarkup(
            parent_pin=self.pin,
            profile=self.profile,
            markup_type=MarkupType.LINE,
            geometry={"type": "LineString", "coordinates": [[0, 0], [1, 1]]},
            **kwargs,
        )

    def test_a_breakout_colour_does_not_survive_a_bulk_restore(self):
        created = PinMarkup.objects.bulk_create([self._unsaved(color=BREAKOUT, border_color=BREAKOUT)])

        reloaded = PinMarkup.objects.get(pk=created[0].pk)
        self.assertEqual(reloaded.color, "#e53e3e")
        self.assertEqual(reloaded.border_color, "")

    def test_valid_colours_are_untouched_by_a_bulk_restore(self):
        created = PinMarkup.objects.bulk_create([self._unsaved(color="#1a2b3c", border_color="none")])

        reloaded = PinMarkup.objects.get(pk=created[0].pk)
        self.assertEqual(reloaded.color, "#1a2b3c")
        self.assertEqual(reloaded.border_color, "none")

    def test_a_generator_of_items_is_still_created(self):
        """The override materializes its argument; a lazy caller must still work."""
        created = PinMarkup.objects.bulk_create(self._unsaved(color=BREAKOUT) for _ in range(2))

        self.assertEqual(len(created), 2)
        self.assertEqual({item.color for item in created}, {"#e53e3e"})


class PinMarkupColorStorageTests(TestCase):
    """save() coerces colours regardless of which write path set them."""

    def setUp(self):
        self.user = baker.make("auth.User")
        self.profile = self.user.profile
        self.pin = baker.make("dashboard.Pin", profile=self.profile)

    def _make(self, **kwargs) -> PinMarkup:
        return PinMarkup.objects.create(
            parent_pin=self.pin,
            profile=self.profile,
            markup_type=MarkupType.LINE,
            geometry={"type": "LineString", "coordinates": [[0, 0], [1, 1]]},
            **kwargs,
        )

    def test_valid_colour_is_kept(self):
        item = self._make(color="#1a2b3c")
        self.assertEqual(item.color, "#1a2b3c")

    def test_breakout_colour_is_replaced_on_create(self):
        item = self._make(color=BREAKOUT)
        self.assertEqual(item.color, "#e53e3e")

    def test_breakout_colour_is_replaced_on_update(self):
        item = self._make(color="#1a2b3c")
        item.color = BREAKOUT
        item.save()
        item.refresh_from_db()
        self.assertEqual(item.color, "#e53e3e")

    def test_breakout_border_colour_is_replaced(self):
        item = self._make(border_color=BREAKOUT)
        self.assertEqual(item.border_color, "")

    def test_border_none_sentinel_survives(self):
        item = self._make(border_color="none")
        self.assertEqual(item.border_color, "none")

    def test_valid_border_colour_survives(self):
        item = self._make(border_color="#0f172a")
        self.assertEqual(item.border_color, "#0f172a")

    def test_unset_border_colour_stays_unset(self):
        item = self._make()
        self.assertEqual(item.border_color, "")

    def test_stored_value_is_what_the_database_returns(self):
        """The coercion is persisted, not just applied to the in-memory object."""
        item = self._make(color=BREAKOUT, border_color=BREAKOUT)
        reloaded = PinMarkup.objects.get(pk=item.pk)
        self.assertEqual(reloaded.color, "#e53e3e")
        self.assertEqual(reloaded.border_color, "")

    def test_snapshot_import_is_covered_too(self):
        """from_snapshot_shape builds an unsaved instance; saving still sanitizes."""
        item = PinMarkup.from_snapshot_shape(
            {"type": "line", "latlngs": [[0, 0], [1, 1]], "color": BREAKOUT},
        )
        self.assertIsNotNone(item)
        assert item is not None
        item.parent_pin = self.pin
        item.profile = self.profile
        item.save()
        self.assertEqual(item.color, "#e53e3e")
