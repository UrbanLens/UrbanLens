"""A name that sanitizes away must be refused, not stored blank."""

from __future__ import annotations

from model_bakery import baker
import pytest

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.aliases.model import PinAlias
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.services.pins.pin_subresources import AliasExistsError, create_pin_alias


class AliasNameValidationTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.profile = Profile.objects.get(user=baker.make("auth.User"))
        self.pin = baker.make(Pin, profile=self.profile, location=baker.make(Location), name="Real name")

    def test_an_ordinary_alias_is_stored(self) -> None:
        """Anchors the rest: rejection must not have become blanket."""
        alias = create_pin_alias(self.pin, name="Old Mill")

        self.assertEqual(alias.name, "Old Mill")

    def test_an_emoji_only_name_is_refused(self) -> None:
        with pytest.raises(ValueError, match="Name is required"):
            create_pin_alias(self.pin, name="\U0001f389\U0001f389")

    def test_a_markup_only_name_is_refused(self) -> None:
        with pytest.raises(ValueError, match="Name is required"):
            create_pin_alias(self.pin, name="<>")

    def test_no_blank_alias_row_is_left_behind(self) -> None:
        """The visible symptom: a blank entry in the pin's alias list."""
        for junk in ("\U0001f389", "<>", "|;"):
            with pytest.raises(ValueError, match="Name is required"):
                create_pin_alias(self.pin, name=junk)

        self.assertFalse(
            PinAlias.objects.filter(pin=self.pin, name="").exists(),
            "a name that sanitized away was stored as a blank alias",
        )

    def test_a_name_containing_stripped_characters_keeps_the_rest(self) -> None:
        """Partial sanitization is still a real name and must be accepted."""
        alias = create_pin_alias(self.pin, name="Old <b>Mill</b>")

        self.assertEqual(alias.name, "Old bMill/b")

    def test_an_underscored_name_survives(self) -> None:
        """Guards the companion fix: underscore is no longer stripped."""
        alias = create_pin_alias(self.pin, name="Site_7")

        self.assertEqual(alias.name, "Site_7")

    def test_a_case_insensitive_duplicate_name_is_refused(self) -> None:
        """The unique constraint this bug hid behind: same name, different case, is still a duplicate."""
        create_pin_alias(self.pin, name="Old Mill")

        with pytest.raises(AliasExistsError):
            create_pin_alias(self.pin, name="OLD MILL")

        # The atomic() savepoint must have rolled the failed insert back cleanly,
        # not left a partial or duplicate row behind.
        self.assertEqual(PinAlias.objects.filter(pin=self.pin, name__iexact="old mill").count(), 1)
