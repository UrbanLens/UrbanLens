"""A background draft wiki must read as "no wiki exists yet" on every surface.

Every pinned Location gets a ``Wiki`` row created ahead of any user action, so
enrichment (Google place linking, name resolution, boundary generation,
Wikipedia seeding) has somewhere to write before anyone clicks "Create Wiki".
``Wiki.officially_created`` is what separates those drafts from real community
pages, and its documented contract is that a draft is invisible everywhere.

That contract was enforced one call site at a time. ``get_for_location`` and
``resolve_visible_wiki`` honour it; several surfaces reached for
``Wiki.objects.filter(...)`` directly and did not, so a draft could be listed
by name in a reference picker, matched by comment search, offered as a
Consensus round, or handed back as a markup target.

These tests pin the invariant per surface rather than trusting each call site
to remember it. The scope they should all route through is
``Wiki.objects.official()``.
"""

from __future__ import annotations

from django.contrib.auth.models import User
from django.utils import timezone
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.wiki.model import Wiki
from urbanlens.dashboard.services.consensus.eligibility import eligible_wikis, eligible_wikis_for_all
from urbanlens.dashboard.services.custom_fields.custom_field_references import referenceable_queryset


class DraftWikiVisibilityTests(TestCase):
    """A draft the viewer has every other claim on is still invisible."""

    def setUp(self) -> None:
        """Pin a location whose wiki is only a draft.

        The viewer deliberately has the strongest possible claim short of the
        wiki being real - their own pin, on the exact Location. Anything that
        still surfaces it is ignoring ``officially_created`` rather than
        failing an access check.
        """
        super().setUp()
        self.user = baker.make(User)
        self.profile = self.user.profile
        self.location = baker.make(Location, latitude=44.0121, longitude=-73.1801)
        self.draft = baker.make(Wiki, location=self.location, name="Draft Only", officially_created=False)
        self.pin = baker.make(Pin, profile=self.profile, location=self.location, parent_pin=None)

    def test_the_reference_picker_does_not_list_it(self) -> None:
        """A custom field's wiki REFERENCE picker offers only real wikis."""
        self.assertNotIn(self.draft, referenceable_queryset("wiki", self.profile))

    def test_consensus_will_not_build_a_round_from_it(self) -> None:
        """A draft is not community content, so it is not playable."""
        Pin.objects.filter(pk=self.pin.pk).update(last_visited=timezone.now())

        self.assertNotIn(self.draft, eligible_wikis(self.profile))
        self.assertNotIn(self.draft, eligible_wikis_for_all([self.profile]))

    def test_official_scope_is_the_one_place_that_decides(self) -> None:
        """Positive control: promoting the draft makes every surface show it.

        Without this, a scope that returned nothing at all would pass the tests
        above.
        """
        Wiki.objects.filter(pk=self.draft.pk).update(officially_created=True)

        self.assertIn(self.draft, referenceable_queryset("wiki", self.profile))
        self.assertIn(self.draft, Wiki.objects.official())
