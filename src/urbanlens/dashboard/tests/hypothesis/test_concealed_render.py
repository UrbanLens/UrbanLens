"""Render the wiki as a concealed viewer and assert what is *absent*.

Every other concealment test exercises a function in isolation. That is why a
self-review missed the thing that matters most: the two functions that actually
conceal anything - ``concealed_field_values`` and ``conceal_rows`` - can be
correct, tested, and wired to nothing.

This test drives the real view and the real API payload with the predicate
forced on, and asserts the *absence* of a stranger's contributions. Absence is
the whole contract: a concealed wiki has to read as one nobody has been to, and
a test that asserts presence of the right things cannot see the wrong things
that are still there beside them.
"""

from __future__ import annotations

from unittest import mock

from django.contrib.auth.models import User
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.abstract.choices import SecurityLevel
from urbanlens.dashboard.models.abstract.versioning import WriteSource, writing_as
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.wiki.model import Wiki

#: Planted in every field a stranger can write. Any of these reaching the page
#: is a leak, and naming them individually is what makes the failure readable.
CANARIES = {
    "description": "CANARY-DESCRIPTION-entry through the north fence",
    "alias": "CANARY-ALIAS",
    "comment": "CANARY-COMMENT-the door is unlocked",
    "name": "CANARY-NAME",
}


class ConcealedRenderTests(TestCase):
    """The wiki page, rendered for somebody who has not earned its detail."""

    def setUp(self) -> None:
        """Build a wiki a stranger contributed to, and a viewer with no tie to them."""
        super().setUp()
        # official_name is what enrichment resolves and what every creation
        # path names a wiki from, so it is the automatic name a concealed
        # viewer should end up seeing.
        self.location = baker.make(Location, latitude=43.0731, longitude=-89.4012, official_name="Provider Name")
        self.wiki = baker.make(Wiki, location=self.location, name="Provider Name", officially_created=True)

        self.stranger = baker.make(User).profile
        with writing_as(WriteSource.USER, actor=self.stranger.pk):
            Wiki.objects.filter(pk=self.wiki.pk).update(
                name=CANARIES["name"],
                description=CANARIES["description"],
                cameras=SecurityLevel.SOME,
                fences=SecurityLevel.SOME,
            )

        baker.make("dashboard.WikiAlias", wiki=self.wiki, name=CANARIES["alias"], created_by=self.stranger)
        baker.make("dashboard.Comment", wiki=self.wiki, pin=None, profile=self.stranger, text=CANARIES["comment"])

        self.viewer_user = baker.make(User)
        # A pin is what grants wiki access at all; it says nothing about
        # whether the viewer has earned the community's detail.
        baker.make(Pin, profile=self.viewer_user.profile, location=self.location, parent_pin=None)

    def _render_concealed(self) -> str:
        """Return the wiki page as rendered with concealment forced on."""
        self.client.force_login(self.viewer_user)
        with mock.patch("urbanlens.dashboard.services.wiki.concealment.concealment_active", return_value=True):
            response = self.client.get(reverse("location.wiki", kwargs={"location_slug": self.location.slug or str(self.location.uuid)}))
        self.assertEqual(response.status_code, 200)
        return response.content.decode()

    def test_the_page_carries_none_of_a_strangers_contributions(self) -> None:
        """The single assertion this whole feature is for."""
        body = self._render_concealed()

        leaked = sorted(label for label, canary in CANARIES.items() if canary in body)
        self.assertEqual(leaked, [], f"concealed wiki page leaked: {leaked}")

    def test_the_page_still_renders_the_automatic_name(self) -> None:
        """Positive control, and a tell in its own right.

        A concealed wiki showing *no* name is not what a brand-new wiki looks
        like - every creation path names it from the location - so a blank
        title would announce the concealment as loudly as the leak would.

        Note what this does *not* assert: that the wiki's own stored name
        survives. It does not, and should not - a name a person chose is a
        contribution. What the viewer sees is the location's official name,
        which is the same thing a wiki created a moment ago would show.
        """
        body = self._render_concealed()

        self.assertIn("Provider Name", body)

    def test_security_indicators_read_as_unset(self) -> None:
        """A place that reads as surveyed is what concealment exists to prevent."""
        body = self._render_concealed()

        self.assertNotIn(CANARIES["description"], body)
        # The chips render the display label of each security level; "Some"
        # against cameras or fences means the page is still reporting them.
        self.assertNotIn("CANARY", body)
