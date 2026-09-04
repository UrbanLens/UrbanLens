"""The safety check-in's wiki-notify toggle must not enumerate wikis.

``SafetyCheckinWikiOptionView`` renders "also notify the <name> community wiki"
for whatever coordinate the create form's destination marker currently sits on.
The lookup behind it (``find_community_wiki``) filters on
the wiki's existence and nothing else - no viewer, no domain check - so the
fragment answered "is there a community wiki at this coordinate?" for any
logged-in caller, naming the wiki, linking to it, and reporting its last edit
and editor count.

That is the exact inference the access model exists to prevent, reachable
without even creating a pin: sweep coordinates, read the names back. Wiki
access is a place-domain rule (``services.wiki.wiki_access``), and a 404 rather
than a 403 is deliberate precisely so the *absence* of a page cannot be told
apart from the absence of permission to see it.

So the toggle must render for the destination only when the caller can already
reach that wiki, and must be byte-identical to "no wiki here" otherwise.
"""

from __future__ import annotations

from django.contrib.auth.models import User
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.wiki.model import Wiki

URL = "/dashboard/safety/wiki-option/"

#: A coordinate no other fixture in the suite uses, so the bounding-box
#: fallback cannot match a location some other test happened to create.
LAT = 42.6526
LNG = -73.7562


class SafetyWikiOptionOracleTests(TestCase):
    """The toggle names a wiki only to someone who could already open it."""

    def setUp(self) -> None:
        """Put one official wiki at a known coordinate, pinned by its owner."""
        super().setUp()
        self.location = baker.make(Location, latitude=LAT, longitude=LNG)
        self.wiki = baker.make(Wiki, location=self.location, name="Sensitive Site")
        self.owner = baker.make(User)
        baker.make(Pin, profile=self.owner.profile, location=self.location, parent_pin=None)

    def _get(self, user: User) -> str:
        """Return the rendered toggle fragment for *user* at the fixture coordinate."""
        self.client.force_login(user)
        response = self.client.get(URL, {"destination_latitude": str(LAT), "destination_longitude": str(LNG)})
        self.assertEqual(response.status_code, 200)
        return response.content.decode()

    def test_a_stranger_is_not_told_the_wiki_exists(self) -> None:
        """A caller with no pin on the place learns nothing from the toggle."""
        stranger = baker.make(User)

        body = self._get(stranger)

        self.assertNotIn("Sensitive Site", body)
        self.assertNotIn("community wiki", body)

    def test_a_stranger_gets_the_same_answer_as_for_empty_space(self) -> None:
        """The gated response is indistinguishable from "no wiki covers this point".

        Asserting only "the name is absent" would still pass if the fragment
        said "a wiki here is hidden from you", which is the same disclosure in
        different words.
        """
        stranger = baker.make(User)

        gated = self._get(stranger)
        self.client.force_login(stranger)
        empty = self.client.get(
            URL, {"destination_latitude": "-40.0", "destination_longitude": "-170.0"}
        ).content.decode()

        self.assertEqual(gated.strip(), empty.strip())

    def test_someone_who_has_pinned_the_place_still_gets_the_toggle(self) -> None:
        """Positive control: the feature still works for the people it is for.

        Without this, deleting the view's body would pass the two tests above.
        """
        body = self._get(self.owner)

        self.assertIn("Sensitive Site", body)
