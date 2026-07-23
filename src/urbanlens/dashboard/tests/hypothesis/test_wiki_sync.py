"""Tests for wiki-sync: automatically mirroring rating/vulnerability/priority/
danger (pin -> wiki, via WikiStatVote) and newly-added aliases (either or both
directions, additive only) between a pin's private details and its community
wiki - see models.pin.signals, models.aliases.signals, and Profile's
sync_* fields.
"""

from __future__ import annotations

import itertools

from django.contrib.auth.models import User
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.forms.settings_form import WikiSyncSettingsForm
from urbanlens.dashboard.models.aliases.model import PinAlias, WikiAlias
from urbanlens.dashboard.models.cache.location_cache import LocationCache
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.profile.meta import SyncAliasesDirection
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.models.reviews.model import Review
from urbanlens.dashboard.models.wiki.model import Wiki
from urbanlens.dashboard.models.wiki_stat_vote.model import WikiStatVote

_coord_counter = itertools.count(1)


def _make_pin_with_wiki(profile, **kwargs) -> Pin:
    """A pin at a fresh Location, with that Location's Wiki already linked."""
    n = next(_coord_counter)
    location = baker.make(Location, latitude=40.0 + n * 0.001, longitude=-74.0 - n * 0.001)
    wiki = baker.make(Wiki, location=location, name="Test Wiki")
    return baker.make(Pin, profile=profile, location=location, wiki=wiki, **kwargs)


class ProfileDefaultsAndGatingTests(TestCase):
    """New-profile defaults and the community_enabled kill switch."""

    def setUp(self) -> None:
        self.user = baker.make(User)
        self.profile = self.user.profile

    def test_stat_sync_defaults_are_on(self) -> None:
        self.assertTrue(self.profile.sync_rating_to_wiki)
        self.assertTrue(self.profile.sync_vulnerability_to_wiki)
        self.assertTrue(self.profile.sync_priority_to_wiki)
        self.assertTrue(self.profile.sync_danger_to_wiki)

    def test_alias_sync_default_is_from_wiki(self) -> None:
        self.assertEqual(self.profile.sync_aliases, SyncAliasesDirection.FROM_WIKI)

    def test_turning_community_off_forces_every_sync_setting_off(self) -> None:
        self.profile.community_enabled = False
        self.profile.save()
        self.profile.refresh_from_db()
        self.assertFalse(self.profile.sync_rating_to_wiki)
        self.assertFalse(self.profile.sync_vulnerability_to_wiki)
        self.assertFalse(self.profile.sync_priority_to_wiki)
        self.assertFalse(self.profile.sync_danger_to_wiki)
        self.assertEqual(self.profile.sync_aliases, SyncAliasesDirection.OFF)

    def test_a_tampered_post_cannot_re_enable_sync_while_community_is_off(self) -> None:
        Profile.objects.filter(pk=self.profile.pk).update(community_enabled=False)
        self.profile.refresh_from_db()
        self.profile.sync_rating_to_wiki = True
        self.profile.sync_aliases = SyncAliasesDirection.BOTH
        self.profile.save()
        self.profile.refresh_from_db()
        self.assertFalse(self.profile.sync_rating_to_wiki)
        self.assertEqual(self.profile.sync_aliases, SyncAliasesDirection.OFF)

    def test_re_enabling_community_does_not_auto_restore_prior_sync_settings(self) -> None:
        """Matches the existing precedent for the gated visibility fields -
        forced-off values stay off until explicitly re-chosen."""
        self.profile.community_enabled = False
        self.profile.save()
        self.profile.community_enabled = True
        self.profile.save()
        self.profile.refresh_from_db()
        self.assertFalse(self.profile.sync_rating_to_wiki)
        self.assertEqual(self.profile.sync_aliases, SyncAliasesDirection.OFF)


class RatingSyncTests(TestCase):
    """Review.rating -> WikiStatVote(field="rating"), one-way, opt-in."""

    def setUp(self) -> None:
        self.user = baker.make(User)
        self.profile = self.user.profile
        self.pin = _make_pin_with_wiki(self.profile)

    def test_saving_a_review_upserts_the_wiki_stat_vote(self) -> None:
        with self.captureOnCommitCallbacks(execute=True):
            Review.objects.create(profile=self.profile, pin=self.pin, rating=4)
        vote = WikiStatVote.objects.get(wiki=self.pin.wiki, profile=self.profile, field="rating")
        self.assertEqual(vote.value, 4)

    def test_updating_a_review_updates_the_existing_vote_not_a_duplicate(self) -> None:
        with self.captureOnCommitCallbacks(execute=True):
            review = Review.objects.create(profile=self.profile, pin=self.pin, rating=2)
        with self.captureOnCommitCallbacks(execute=True):
            review.rating = 5
            review.save()
        self.assertEqual(WikiStatVote.objects.filter(wiki=self.pin.wiki, profile=self.profile, field="rating").count(), 1)
        self.assertEqual(WikiStatVote.objects.get(wiki=self.pin.wiki, profile=self.profile, field="rating").value, 5)

    def test_deleting_a_review_removes_the_vote(self) -> None:
        with self.captureOnCommitCallbacks(execute=True):
            review = Review.objects.create(profile=self.profile, pin=self.pin, rating=3)
        with self.captureOnCommitCallbacks(execute=True):
            review.delete()
        self.assertFalse(WikiStatVote.objects.filter(wiki=self.pin.wiki, profile=self.profile, field="rating").exists())

    def test_disabled_setting_prevents_the_sync(self) -> None:
        Profile.objects.filter(pk=self.profile.pk).update(sync_rating_to_wiki=False)
        # Re-fetch: self.pin's cached .profile is the pre-update Python object,
        # and the signal reads pin.profile.sync_rating_to_wiki off whatever
        # object Review.objects.create(pin=...) is handed below.
        pin = Pin.objects.get(pk=self.pin.pk)
        with self.captureOnCommitCallbacks(execute=True):
            Review.objects.create(profile=self.profile, pin=pin, rating=4)
        self.assertFalse(WikiStatVote.objects.filter(wiki=self.pin.wiki, profile=self.profile, field="rating").exists())

    def test_no_wiki_means_no_vote_and_no_crash(self) -> None:
        location = baker.make(Location, latitude=41.0, longitude=-75.0)
        pin = baker.make(Pin, profile=self.profile, location=location)
        with self.captureOnCommitCallbacks(execute=True):
            Review.objects.create(profile=self.profile, pin=pin, rating=4)
        self.assertFalse(WikiStatVote.objects.filter(profile=self.profile, field="rating").exists())


class PinStatSyncTests(TestCase):
    """Pin.vulnerability/priority/danger -> WikiStatVote, one-way, opt-in per field."""

    def setUp(self) -> None:
        self.user = baker.make(User)
        self.profile = self.user.profile
        self.pin = _make_pin_with_wiki(self.profile)

    def test_full_save_syncs_all_three_fields(self) -> None:
        self.pin.vulnerability = 3
        self.pin.priority = 4
        self.pin.danger = 5
        with self.captureOnCommitCallbacks(execute=True):
            self.pin.save()
        votes = {v.field: v.value for v in WikiStatVote.objects.filter(wiki=self.pin.wiki, profile=self.profile)}
        self.assertEqual(votes, {"vulnerability": 3, "priority": 4, "danger": 5})

    def test_partial_save_only_syncs_the_touched_field(self) -> None:
        self.pin.danger = 5
        with self.captureOnCommitCallbacks(execute=True):
            self.pin.save(update_fields=["danger", "updated"])
        self.assertTrue(WikiStatVote.objects.filter(wiki=self.pin.wiki, profile=self.profile, field="danger").exists())
        self.assertFalse(WikiStatVote.objects.filter(wiki=self.pin.wiki, profile=self.profile, field="priority").exists())

    def test_per_field_opt_out_is_independent(self) -> None:
        Profile.objects.filter(pk=self.profile.pk).update(sync_danger_to_wiki=False)
        # Re-fetch: self.pin's cached .profile predates the update() above.
        self.pin = Pin.objects.get(pk=self.pin.pk)
        self.pin.priority = 2
        self.pin.danger = 5
        with self.captureOnCommitCallbacks(execute=True):
            self.pin.save(update_fields=["priority", "danger", "updated"])
        self.assertTrue(WikiStatVote.objects.filter(wiki=self.pin.wiki, profile=self.profile, field="priority").exists())
        self.assertFalse(WikiStatVote.objects.filter(wiki=self.pin.wiki, profile=self.profile, field="danger").exists())

    def test_dropping_a_value_to_zero_clears_the_vote(self) -> None:
        self.pin.priority = 3
        with self.captureOnCommitCallbacks(execute=True):
            self.pin.save(update_fields=["priority", "updated"])
        self.assertTrue(WikiStatVote.objects.filter(wiki=self.pin.wiki, profile=self.profile, field="priority").exists())

        self.pin.priority = 0
        with self.captureOnCommitCallbacks(execute=True):
            self.pin.save(update_fields=["priority", "updated"])
        self.assertFalse(WikiStatVote.objects.filter(wiki=self.pin.wiki, profile=self.profile, field="priority").exists())

    def test_no_wiki_means_no_vote_and_no_crash(self) -> None:
        location = baker.make(Location, latitude=42.0, longitude=-76.0)
        pin = baker.make(Pin, profile=self.profile, location=location, vulnerability=3)
        with self.captureOnCommitCallbacks(execute=True):
            pin.save(update_fields=["vulnerability", "updated"])
        self.assertFalse(WikiStatVote.objects.filter(profile=self.profile, field="vulnerability").exists())


class PinAliasToWikiSyncTests(TestCase):
    """A newly-added pin alias mirrors onto the pin's wiki, additive only."""

    def setUp(self) -> None:
        self.user = baker.make(User)
        self.profile = self.user.profile
        self.pin = _make_pin_with_wiki(self.profile)

    def test_to_wiki_creates_a_matching_wiki_alias(self) -> None:
        Profile.objects.filter(pk=self.profile.pk).update(sync_aliases=SyncAliasesDirection.TO_WIKI)
        with self.captureOnCommitCallbacks(execute=True):
            PinAlias.objects.create(pin=self.pin, name="The Old Mill")
        self.assertTrue(WikiAlias.objects.filter(wiki=self.pin.wiki, name="The Old Mill").exists())

    def test_both_direction_also_creates_a_matching_wiki_alias(self) -> None:
        Profile.objects.filter(pk=self.profile.pk).update(sync_aliases=SyncAliasesDirection.BOTH)
        with self.captureOnCommitCallbacks(execute=True):
            PinAlias.objects.create(pin=self.pin, name="Mill House")
        self.assertTrue(WikiAlias.objects.filter(wiki=self.pin.wiki, name="Mill House").exists())

    def test_from_wiki_only_does_not_sync_to_wiki(self) -> None:
        Profile.objects.filter(pk=self.profile.pk).update(sync_aliases=SyncAliasesDirection.FROM_WIKI)
        with self.captureOnCommitCallbacks(execute=True):
            PinAlias.objects.create(pin=self.pin, name="Private Name")
        self.assertFalse(WikiAlias.objects.filter(wiki=self.pin.wiki, name="Private Name").exists())

    def test_off_does_not_sync(self) -> None:
        Profile.objects.filter(pk=self.profile.pk).update(sync_aliases=SyncAliasesDirection.OFF)
        with self.captureOnCommitCallbacks(execute=True):
            PinAlias.objects.create(pin=self.pin, name="Nothing Happens")
        self.assertFalse(WikiAlias.objects.filter(wiki=self.pin.wiki, name="Nothing Happens").exists())

    def test_does_not_duplicate_an_alias_already_on_the_wiki(self) -> None:
        Profile.objects.filter(pk=self.profile.pk).update(sync_aliases=SyncAliasesDirection.TO_WIKI)
        WikiAlias.objects.create(wiki=self.pin.wiki, name="Shared Name")
        with self.captureOnCommitCallbacks(execute=True):
            PinAlias.objects.create(pin=self.pin, name="Shared Name")
        self.assertEqual(WikiAlias.objects.filter(wiki=self.pin.wiki, name="Shared Name").count(), 1)

    def test_deleting_a_pin_alias_never_removes_the_wiki_alias(self) -> None:
        Profile.objects.filter(pk=self.profile.pk).update(sync_aliases=SyncAliasesDirection.TO_WIKI)
        with self.captureOnCommitCallbacks(execute=True):
            alias = PinAlias.objects.create(pin=self.pin, name="Stays On Wiki")
        with self.captureOnCommitCallbacks(execute=True):
            alias.delete()
        self.assertTrue(WikiAlias.objects.filter(wiki=self.pin.wiki, name="Stays On Wiki").exists())

    def test_renaming_an_existing_pin_alias_does_not_sync(self) -> None:
        """Only genuine new aliases sync - edits are explicitly out of scope."""
        Profile.objects.filter(pk=self.profile.pk).update(sync_aliases=SyncAliasesDirection.TO_WIKI)
        with self.captureOnCommitCallbacks(execute=True):
            alias = PinAlias.objects.create(pin=self.pin, name="Original")
        with self.captureOnCommitCallbacks(execute=True):
            alias.name = "Renamed"
            alias.save()
        self.assertFalse(WikiAlias.objects.filter(wiki=self.pin.wiki, name="Renamed").exists())
        # The original sync from creation is untouched by the rename.
        self.assertTrue(WikiAlias.objects.filter(wiki=self.pin.wiki, name="Original").exists())

    def test_no_wiki_means_no_sync_and_no_crash(self) -> None:
        Profile.objects.filter(pk=self.profile.pk).update(sync_aliases=SyncAliasesDirection.TO_WIKI)
        location = baker.make(Location, latitude=43.0, longitude=-77.0)
        # Fresh fetch: pin.profile must reflect the sync_aliases update() above.
        profile = Profile.objects.get(pk=self.profile.pk)
        pin = baker.make(Pin, profile=profile, location=location)
        with self.captureOnCommitCallbacks(execute=True):
            PinAlias.objects.create(pin=pin, name="No Wiki Yet")
        # setUp's _make_pin_with_wiki already creates a WikiAlias for the
        # wiki's own name (Wiki.save()'s pre-existing invariant) - assert
        # against that specific alias name, not table-wide existence.
        self.assertFalse(WikiAlias.objects.filter(name="No Wiki Yet").exists())


class WikiAliasToPinSyncTests(TestCase):
    """A newly-added wiki alias mirrors onto every opted-in profile's pin at that location."""

    def setUp(self) -> None:
        self.owner_user = baker.make(User)
        self.owner_profile = self.owner_user.profile
        self.pin = _make_pin_with_wiki(self.owner_profile)
        self.wiki = self.pin.wiki

    def test_from_wiki_creates_a_matching_pin_alias(self) -> None:
        Profile.objects.filter(pk=self.owner_profile.pk).update(sync_aliases=SyncAliasesDirection.FROM_WIKI)
        with self.captureOnCommitCallbacks(execute=True):
            WikiAlias.objects.create(wiki=self.wiki, name="Community Name")
        self.assertTrue(PinAlias.objects.filter(pin=self.pin, name="Community Name").exists())

    def test_both_direction_also_creates_a_matching_pin_alias(self) -> None:
        Profile.objects.filter(pk=self.owner_profile.pk).update(sync_aliases=SyncAliasesDirection.BOTH)
        with self.captureOnCommitCallbacks(execute=True):
            WikiAlias.objects.create(wiki=self.wiki, name="Both Ways")
        self.assertTrue(PinAlias.objects.filter(pin=self.pin, name="Both Ways").exists())

    def test_to_wiki_only_does_not_sync_from_wiki(self) -> None:
        Profile.objects.filter(pk=self.owner_profile.pk).update(sync_aliases=SyncAliasesDirection.TO_WIKI)
        with self.captureOnCommitCallbacks(execute=True):
            WikiAlias.objects.create(wiki=self.wiki, name="Not For Me")
        self.assertFalse(PinAlias.objects.filter(pin=self.pin, name="Not For Me").exists())

    def test_off_does_not_sync(self) -> None:
        Profile.objects.filter(pk=self.owner_profile.pk).update(sync_aliases=SyncAliasesDirection.OFF)
        with self.captureOnCommitCallbacks(execute=True):
            WikiAlias.objects.create(wiki=self.wiki, name="Nothing Happens")
        self.assertFalse(PinAlias.objects.filter(pin=self.pin, name="Nothing Happens").exists())

    def test_only_opted_in_pins_at_a_shared_location_receive_it(self) -> None:
        """Two different users each have a pin at the same location; only the
        one with sync_aliases enabled picks up a new wiki alias."""
        other_user = baker.make(User)
        other_profile = other_user.profile
        Profile.objects.filter(pk=other_profile.pk).update(sync_aliases=SyncAliasesDirection.FROM_WIKI)
        other_pin = baker.make(Pin, profile=other_profile, location=self.pin.location, wiki=self.wiki)

        third_user = baker.make(User)
        third_profile = third_user.profile
        Profile.objects.filter(pk=third_profile.pk).update(sync_aliases=SyncAliasesDirection.OFF)
        third_pin = baker.make(Pin, profile=third_profile, location=self.pin.location, wiki=self.wiki)

        Profile.objects.filter(pk=self.owner_profile.pk).update(sync_aliases=SyncAliasesDirection.OFF)
        with self.captureOnCommitCallbacks(execute=True):
            WikiAlias.objects.create(wiki=self.wiki, name="Selective Sync")

        self.assertTrue(PinAlias.objects.filter(pin=other_pin, name="Selective Sync").exists())
        self.assertFalse(PinAlias.objects.filter(pin=third_pin, name="Selective Sync").exists())
        self.assertFalse(PinAlias.objects.filter(pin=self.pin, name="Selective Sync").exists())

    def test_deleting_a_wiki_alias_never_removes_the_pin_alias(self) -> None:
        Profile.objects.filter(pk=self.owner_profile.pk).update(sync_aliases=SyncAliasesDirection.FROM_WIKI)
        with self.captureOnCommitCallbacks(execute=True):
            alias = WikiAlias.objects.create(wiki=self.wiki, name="Stays On Pin")
        with self.captureOnCommitCallbacks(execute=True):
            alias.delete()
        self.assertTrue(PinAlias.objects.filter(pin=self.pin, name="Stays On Pin").exists())

    def test_renaming_an_existing_wiki_alias_does_not_sync(self) -> None:
        Profile.objects.filter(pk=self.owner_profile.pk).update(sync_aliases=SyncAliasesDirection.FROM_WIKI)
        with self.captureOnCommitCallbacks(execute=True):
            alias = WikiAlias.objects.create(wiki=self.wiki, name="Original")
        with self.captureOnCommitCallbacks(execute=True):
            alias.name = "Renamed"
            alias.save()
        self.assertFalse(PinAlias.objects.filter(pin=self.pin, name="Renamed").exists())

    def test_both_ways_round_trip_does_not_loop_or_duplicate(self) -> None:
        """Adding a pin alias with BOTH enabled mirrors to the wiki, which then
        tries to mirror back to the same pin - get_or_create's idempotency
        must stop this at one row each, not recurse or duplicate."""
        Profile.objects.filter(pk=self.owner_profile.pk).update(sync_aliases=SyncAliasesDirection.BOTH)
        with self.captureOnCommitCallbacks(execute=True):
            PinAlias.objects.create(pin=self.pin, name="Round Trip")
        self.assertEqual(PinAlias.objects.filter(pin=self.pin, name="Round Trip").count(), 1)
        self.assertEqual(WikiAlias.objects.filter(wiki=self.wiki, name="Round Trip").count(), 1)


class WikiSyncSettingsFormTests(TestCase):
    """The settings form's fields and its community-off disable behavior."""

    def setUp(self) -> None:
        self.user = baker.make(User)
        self.profile = self.user.profile

    def test_valid_submission_saves_every_field(self) -> None:
        form = WikiSyncSettingsForm(
            {
                "sync_rating_to_wiki": "on",
                "sync_vulnerability_to_wiki": "",
                "sync_priority_to_wiki": "on",
                "sync_danger_to_wiki": "",
                "sync_aliases": "both",
            },
            instance=self.profile,
        )
        self.assertTrue(form.is_valid(), form.errors)
        form.save()
        self.profile.refresh_from_db()
        self.assertTrue(self.profile.sync_rating_to_wiki)
        self.assertFalse(self.profile.sync_vulnerability_to_wiki)
        self.assertEqual(self.profile.sync_aliases, SyncAliasesDirection.BOTH)

    def test_fields_disabled_when_community_is_off(self) -> None:
        Profile.objects.filter(pk=self.profile.pk).update(community_enabled=False)
        self.profile.refresh_from_db()
        form = WikiSyncSettingsForm(instance=self.profile)
        for field in form.fields.values():
            self.assertTrue(field.disabled)


class SettingsViewWikiSyncSectionTests(TestCase):
    """POST /settings/ section=wiki_sync end to end."""

    def setUp(self) -> None:
        self.user = baker.make(User)
        self.profile = self.user.profile
        self.client.force_login(self.user)

    def test_settings_page_renders_the_section(self) -> None:
        response = self.client.get(reverse("settings.view"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'id="wiki-sync-settings-section"')
        self.assertContains(response, "sync_aliases")

    def test_posting_the_section_saves_it(self) -> None:
        response = self.client.post(
            reverse("settings.view"),
            {"section": "wiki_sync", "sync_rating_to_wiki": "on", "sync_aliases": "off"},
        )
        self.assertEqual(response.status_code, 302)
        self.profile.refresh_from_db()
        self.assertTrue(self.profile.sync_rating_to_wiki)
        self.assertFalse(self.profile.sync_vulnerability_to_wiki)
        self.assertEqual(self.profile.sync_aliases, SyncAliasesDirection.OFF)


class MediaCacheInvalidationOnNewAliasTests(TestCase):
    """A new pin/wiki alias may surface a name-quality-dependent match (a
    Wikipedia article, or images on one) that couldn't be found under the
    previous name set - see docs/prompts/completed.md's "Wikipedia article
    images not reliably reaching Media section" entry. A genuinely new alias
    should clear the location's Wikipedia/Wikimedia LocationCache rows so the
    next panel view does a fresh lookup; renaming an existing alias should not.
    """

    def setUp(self) -> None:
        self.user = baker.make(User)
        self.profile = self.user.profile
        self.pin = _make_pin_with_wiki(self.profile)

    def _seed_cache(self) -> None:
        for source in ("wikipedia", "wikimedia", "wikipedia_media", "nominatim"):
            LocationCache.objects.update_or_create(location=self.pin.location, source=source, defaults={"data": {"stub": True}})

    def _cached_sources(self) -> set[str]:
        return set(LocationCache.objects.filter(location=self.pin.location).values_list("source", flat=True))

    def test_new_pin_alias_clears_name_sensitive_caches_but_not_others(self) -> None:
        self._seed_cache()
        with self.captureOnCommitCallbacks(execute=True):
            PinAlias.objects.create(pin=self.pin, name="New Alias")
        self.assertEqual(self._cached_sources(), {"nominatim"})

    def test_renaming_an_existing_pin_alias_does_not_clear_the_cache(self) -> None:
        with self.captureOnCommitCallbacks(execute=True):
            alias = PinAlias.objects.create(pin=self.pin, name="Original")
        # Creation above already cleared the seeded rows (tested separately) -
        # reseed to isolate the rename itself.
        self._seed_cache()
        with self.captureOnCommitCallbacks(execute=True):
            alias.name = "Renamed"
            alias.save()
        self.assertEqual(self._cached_sources(), {"wikipedia", "wikimedia", "wikipedia_media", "nominatim"})

    def test_new_wiki_alias_clears_name_sensitive_caches_but_not_others(self) -> None:
        self._seed_cache()
        with self.captureOnCommitCallbacks(execute=True):
            WikiAlias.objects.create(wiki=self.pin.wiki, name="New Wiki Alias")
        self.assertEqual(self._cached_sources(), {"nominatim"})

    def test_new_pin_alias_with_no_cached_data_yet_does_not_crash(self) -> None:
        location = baker.make(Location, latitude=44.0, longitude=-78.0)
        pin = baker.make(Pin, profile=self.profile, location=location)
        with self.captureOnCommitCallbacks(execute=True):
            PinAlias.objects.create(pin=pin, name="No Cache Yet")
        self.assertEqual(LocationCache.objects.filter(location=location).count(), 0)
