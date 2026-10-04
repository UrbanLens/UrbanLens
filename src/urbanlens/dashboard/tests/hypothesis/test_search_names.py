"""Which of a pin's names are its owner's own, and which cached search each set of them reads (P188)."""

from __future__ import annotations

from django.contrib.auth.models import User
from model_bakery import baker

from urbanlens.core.tests.testcase import SimpleTestCase, TestCase
from urbanlens.dashboard.models.aliases.model import AliasType, PinAlias, WikiAlias
from urbanlens.dashboard.models.cache.location_cache import LocationCache
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.models.wiki.model import Wiki
from urbanlens.dashboard.services.pins.search_names import SearchNames, search_names

OFFICIAL = "Hudson River State Hospital"


class AudienceKeyTests(SimpleTestCase):
    def test_no_custom_names_reads_the_shared_search(self) -> None:
        names = SearchNames(shared=(OFFICIAL,), custom=(), context=())

        self.assertEqual(names.audience, "")
        self.assertIsNone(names.own)
        self.assertEqual([scope.audience for scope in names.scopes], [""])

    def test_the_same_set_in_any_order_or_case_is_one_audience(self) -> None:
        dolby = SearchNames(shared=(OFFICIAL,), custom=("HRSH", "Blueberry"), context=())
        casey = SearchNames(shared=(OFFICIAL,), custom=("blueberry", " hrsh "), context=())

        self.assertEqual(dolby.audience, casey.audience)
        self.assertEqual(len(dolby.audience), 64)

    def test_a_different_set_is_a_different_audience(self) -> None:
        fred = SearchNames(shared=(OFFICIAL,), custom=("Apple", "Blueberry"), context=())
        dolby = SearchNames(shared=(OFFICIAL,), custom=("HRSH", "Blueberry"), context=())
        mildred = SearchNames(shared=(OFFICIAL,), custom=("HRSH",), context=())

        self.assertEqual(len({fred.audience, dolby.audience, mildred.audience}), 3)

    def test_the_shared_search_uses_only_shared_names(self) -> None:
        names = SearchNames(shared=(OFFICIAL,), custom=("Secret Ward",), context=("Campus",))

        self.assertEqual(names.base.names, (OFFICIAL,))
        self.assertEqual(names.base.context, ())
        self.assertEqual(names.own.names if names.own else None, ("secret ward",))
        self.assertEqual(names.own.context if names.own else None, ("campus",))

    def test_the_sites_a_pin_is_filed_under_are_its_owners_too(self) -> None:
        nested = SearchNames(shared=(OFFICIAL,), custom=(), context=("Campus",))

        self.assertNotEqual(nested.audience, "")
        self.assertEqual(nested.own.names if nested.own else None, (OFFICIAL,))

    def test_a_nested_search_of_the_shared_names_follows_them(self) -> None:
        """Its row is built from the shared names, so a new shared name must not find the old row fresh."""
        before = SearchNames(shared=(OFFICIAL,), custom=(), context=("Campus",))
        after = SearchNames(shared=(OFFICIAL, "Kirkbride"), custom=(), context=("Campus",))

        self.assertNotEqual(before.audience, after.audience)

    def test_an_owner_s_own_names_keep_their_search_whatever_the_shared_names(self) -> None:
        before = SearchNames(shared=(OFFICIAL,), custom=("Secret Ward",), context=("Campus",))
        after = SearchNames(shared=(OFFICIAL, "Kirkbride"), custom=("Secret Ward",), context=("Campus",))

        self.assertEqual(before.audience, after.audience)

    def test_a_set_is_searched_in_the_same_order_whoever_holds_it(self) -> None:
        one = SearchNames(shared=(), custom=("HRSH", "Blueberry"), context=())
        other = SearchNames(shared=(), custom=("Blueberry", "HRSH"), context=())

        self.assertEqual(one.own, other.own)


class SearchNamesForPinTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.location = baker.make(
            Location, official_name=OFFICIAL, official_name_source="historic_register", locality="Poughkeepsie"
        )
        self.wiki = baker.make(Wiki, location=self.location, name="HRSH Campus")
        WikiAlias.objects.create(wiki=self.wiki, name="Kirkbride")
        WikiAlias.objects.create(wiki=self.wiki, name="The Castle", kind=AliasType.NICKNAME)
        self.profile = Profile.objects.get(user=baker.make(User))

    def pin(self, *aliases: str, **fields) -> Pin:
        pin = baker.make(Pin, profile=self.profile, location=self.location, **fields)
        for alias in aliases:
            PinAlias.objects.create(pin=pin, name=alias)
        return pin

    def test_shared_names_are_the_places_official_and_wiki_names(self) -> None:
        self.assertEqual(search_names(self.pin()).shared, (OFFICIAL, "HRSH Campus", "Kirkbride"))

    def test_an_official_name_of_unknown_origin_is_not_shared(self) -> None:
        """P186: a legacy name no provider is recorded for may be a person's own text, e.g. a pin name."""
        Location.objects.filter(pk=self.location.pk).update(official_name_source="")
        self.location.refresh_from_db()

        names = search_names(self.pin())

        self.assertNotIn(OFFICIAL, names.shared)
        self.assertEqual(names.shared, ("HRSH Campus", "Kirkbride"))

    def test_the_pins_name_and_aliases_are_custom(self) -> None:
        names = search_names(self.pin("Blueberry", name="Ward Seven"))

        self.assertEqual(set(names.custom), {"ward seven", "blueberry"})

    def test_a_name_restating_a_shared_one_is_not_custom(self) -> None:
        names = search_names(self.pin("  hudson RIVER   state hospital", "KIRKBRIDE", name="hrsh campus"))

        self.assertEqual(names.custom, ())
        self.assertEqual(names.audience, "")

    def test_nicknames_are_never_search_names(self) -> None:
        pin = self.pin()
        PinAlias.objects.create(pin=pin, name="Old Spooky", kind=AliasType.NICKNAME)

        names = search_names(pin)

        self.assertNotIn("Old Spooky", names.custom)
        self.assertNotIn("The Castle", names.shared)

    def test_a_parent_pins_names_are_context_for_the_owners_search(self) -> None:
        parent = baker.make(Pin, profile=self.profile, location=baker.make(Location), name="West Campus")
        child = self.pin(parent_pin=parent)

        names = search_names(child)

        self.assertEqual(names.context, ("West Campus",))
        self.assertEqual(names.base.context, ())
        self.assertNotEqual(names.audience, "")

    def test_a_new_wiki_alias_leaves_no_nested_search_of_the_old_names_fresh(self) -> None:
        parent = baker.make(Pin, profile=self.profile, location=baker.make(Location), name="West Campus")
        child = self.pin(parent_pin=parent, name=OFFICIAL)
        LocationCache.set(self.location, "wikimedia", {"items": []}, audience=search_names(child).audience)

        with self.captureOnCommitCallbacks(execute=True):
            WikiAlias.objects.create(wiki=self.wiki, name="Ward Building Annex")

        self.assertIsNone(LocationCache.get_fresh(self.location, "wikimedia", search_names(child).audience))
