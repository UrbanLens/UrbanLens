"""A wiki created from a pin is named after the place, not its postal address.

Reported from staging: creating a community wiki from a pin called "HRSH", with
the aliases "Hudson Heritage", "Hudson River State Hospital" and "HRSH"
explicitly selected in the dialog, produced a wiki titled "83 Hudson View Dr,
Poughkeepsie, NY 12601, USA".

``Wiki.objects.get_or_create_for_location`` names a new wiki ``location.official_name``,
which for a reverse-geocoded location is the street address. That is a
reasonable default when nothing else is known - it is used for background draft
creation, where there is no pin - but the pin-driven path knows two better
things and was discarding both.

Preference order is what the user has said about the place, most direct first:
the name they gave their own pin, then an alias they chose in the dialog. An
address still wins over a placeholder.
"""

from __future__ import annotations

from django.contrib.auth.models import User
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.aliases.model import AliasType
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.services.wiki.wiki_share import WikiShareService

_ADDRESS = "83 Hudson View Dr, Poughkeepsie, NY 12601, USA"


class WikiCreationNamingTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # absorbs the bootstrap site-admin promotion
        self.profile = baker.make(User).profile
        self._seq = 0

    def _pin(self, *, name: str | None) -> Pin:
        self._seq += 1
        location = baker.make(Location, latitude=41.73332 + self._seq / 10000, longitude=-73.92794, official_name=_ADDRESS)
        return baker.make(Pin, profile=self.profile, location=location, parent_pin=None, name=name)

    def _alias(self, pin: Pin, name: str, kind: str = AliasType.ALTERNATE):
        return baker.make("dashboard.PinAlias", pin=pin, name=name, kind=kind)

    def test_a_pin_name_is_never_published_to_the_wiki(self) -> None:
        """A pin name is the user's private label; only what they shared is used.

        This is a standing decision of the service (see
        ``test_pin_name_is_never_seeded_onto_wiki``) and naming must not become a
        way around it - the reported case works through the alias the user chose,
        which happened to carry the same text.
        """
        pin = self._pin(name="HRSH")

        wiki, _shared = WikiShareService().share_from_pin(pin)

        self.assertEqual(wiki.name, _ADDRESS, "an unshared pin name must not become the public wiki title")

    def test_a_chosen_alias_wins_over_the_address(self) -> None:
        """The reported case: aliases picked in the dialog are what the user handed over."""
        pin = self._pin(name="HRSH")
        # The pin's name already exists as an alias - that is how a private name
        # reaches the public side at all, and it is what the user selects.
        alias = pin.aliases.get(name="HRSH")

        wiki, _created = WikiShareService().share_from_pin(pin, alias_ids={alias.pk})

        self.assertEqual(wiki.name, "HRSH")

    def test_a_chosen_alias_is_used_when_the_pin_is_unnamed(self) -> None:
        pin = self._pin(name=None)
        alias = self._alias(pin, "Hudson River State Hospital")

        wiki, _created = WikiShareService().share_from_pin(pin, alias_ids={alias.pk})

        self.assertEqual(wiki.name, "Hudson River State Hospital")

    def test_an_address_stands_when_nothing_was_shared(self) -> None:
        """The claimed name stands when the user handed over nothing better."""
        pin = self._pin(name="Unnamed Location")

        wiki, _created = WikiShareService().share_from_pin(pin)

        self.assertEqual(wiki.name, _ADDRESS)

    def test_the_chosen_aliases_are_still_seeded(self) -> None:
        """Naming from an alias must not replace copying them in."""
        pin = self._pin(name="HRSH")
        alias = self._alias(pin, "Hudson Heritage")

        wiki, _created = WikiShareService().share_from_pin(pin, alias_ids={alias.pk})

        # Named from the alias the user selected, and that alias is still copied
        # across - naming from one must not replace seeding them.
        self.assertEqual(wiki.name, "Hudson Heritage")
        self.assertTrue(wiki.aliases.filter(name="Hudson Heritage").exists(), "the selected alias should still be copied onto the wiki")
