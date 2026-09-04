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

#: Provider-supplied text a brand-new wiki would carry. Its *presence* is the
#: only assertion in this file that fails if field resolution regresses.
AUTOMATIC_BLURB = "AUTOMATIC-BLURB-relayed-from-a-provider"

#: Planted in every field a stranger can write. Any of these reaching the page
#: is a leak, and naming them individually is what makes the failure readable.
#: Only `name` and `description` render in the page response - aliases and
#: comments load over HTMX from their own endpoints, so those two are asserted
#: in ConcealedPanelTests, not here (V7).
CANARIES = {
    "description": "CANARY-DESCRIPTION-entry through the north fence",
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
        self.wiki = baker.make(Wiki, location=self.location, name="Provider Name")

        # A real automatic write. Without one, every field falls to its model
        # default and resolve_fields is never exercised - the whole file passed
        # with it stubbed to return {}, which is how this went unnoticed.
        # baker.make() runs outside a request or task, so its create rows record
        # as SYSTEM, which qualifies for nobody.
        with writing_as(WriteSource.AUTOMATIC):
            Wiki.objects.filter(pk=self.wiki.pk).update(description=AUTOMATIC_BLURB)

        self.stranger = baker.make(User).profile
        with writing_as(WriteSource.USER, actor=self.stranger.pk):
            Wiki.objects.filter(pk=self.wiki.pk).update(
                name=CANARIES["name"],
                description=CANARIES["description"],
                cameras=SecurityLevel.SOME,
                fences=SecurityLevel.SOME,
            )

        # Kept as fixtures so the wiki under test is a realistically populated
        # one, but deliberately not in CANARIES: both load over HTMX from their
        # own endpoints, so asserting them against the page response would be
        # asserting an absence the page was never going to contain.
        # ConcealedPanelTests covers them where they actually render.
        baker.make("dashboard.WikiAlias", wiki=self.wiki, name="PANEL-ONLY-ALIAS", created_by=self.stranger)
        baker.make("dashboard.Comment", wiki=self.wiki, pin=None, profile=self.stranger, text="PANEL-ONLY-COMMENT")

        self.viewer_user = baker.make(User)
        # A pin is what grants wiki access at all; it says nothing about
        # whether the viewer has earned the community's detail.
        baker.make(Pin, profile=self.viewer_user.profile, location=self.location, parent_pin=None)

    def _render_concealed(self) -> str:
        """Return the wiki page as rendered with concealment forced on."""
        self.client.force_login(self.viewer_user)
        with mock.patch("urbanlens.dashboard.services.wiki.concealment.concealment_active", return_value=True):
            response = self.client.get(
                reverse("location.wiki", kwargs={"location_slug": self.location.slug or str(self.location.uuid)})
            )
        self.assertEqual(response.status_code, 200)
        return response.content.decode()

    def test_the_page_carries_none_of_a_strangers_contributions(self) -> None:
        """The single assertion this whole feature is for."""
        body = self._render_concealed()

        leaked = sorted(label for label, canary in CANARIES.items() if canary in body)
        self.assertEqual(leaked, [], f"concealed wiki page leaked: {leaked}")

    def test_provider_content_still_reaches_the_page(self) -> None:
        """The one assertion here that fails if resolve_fields regresses.

        Rule 2 is "show automatically fetched content", and until this existed
        nothing tested it: every other assertion in this file is negative, so
        stubbing resolve_fields to return {} left them all green while
        concealing everything including the provider data a brand-new wiki
        carries.
        """
        self.assertIn(AUTOMATIC_BLURB, self._render_concealed())

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

    def test_security_indicators_resolve_to_unknown(self) -> None:
        """Rule 3, asserted on the values rather than on the rendered page.

        The About card suppresses itself entirely when a concealed wiki has no
        description, dates or links, so the chips are unreachable from the
        rendered HTML and an assertion there proves nothing. The checkable
        claim is that every one of the eight resolves to exactly UNKNOWN -
        `assertNotEqual(..., SOME)` would be satisfied by None, which renders a
        chip, because the template shows anything that is not the literal
        "unknown".
        """
        from urbanlens.dashboard.services.wiki.concealment import ALWAYS_UNSET, concealed_field_values

        values = concealed_field_values(Wiki.objects.get(pk=self.wiki.pk), self.viewer_user.profile)

        for field_name in ALWAYS_UNSET:
            self.assertEqual(values[field_name], SecurityLevel.UNKNOWN, f"{field_name} must read as unset")


class ConcealedHistoryTests(TestCase):
    """The edit history names who changed what, and carries prior values."""

    def setUp(self) -> None:
        super().setUp()
        self.location = baker.make(Location, latitude=44.9778, longitude=-93.2650, official_name="Provider Name")
        self.wiki = baker.make(Wiki, location=self.location, name="Provider Name")
        self.stranger = baker.make(User).profile
        self.viewer_user = baker.make(User)
        baker.make(Pin, profile=self.viewer_user.profile, location=self.location, parent_pin=None)

    def _history(self) -> str:
        self.client.force_login(self.viewer_user)
        with mock.patch("urbanlens.dashboard.services.wiki.concealment.concealment_active", return_value=True):
            response = self.client.get(
                reverse(
                    "location.wiki.history", kwargs={"location_slug": self.location.slug or str(self.location.uuid)}
                )
            )
        self.assertEqual(response.status_code, 200)
        return response.content.decode()

    def test_a_strangers_edit_is_absent(self) -> None:
        """The list is the record of who has been here."""
        baker.make(
            "dashboard.WikiEdit",
            wiki=self.wiki,
            editor=self.stranger,
            changes={"description": {"from": "", "to": "CANARY-STRANGER-EDIT"}},
            reverted=False,
        )

        self.assertNotIn("CANARY-STRANGER-EDIT", self._history())

    def test_your_own_edit_does_not_carry_the_hidden_prior_value(self) -> None:
        """The leak a read gate cannot close.

        Your own edit row is content the rules promise always to show you - and
        its "from" side holds whatever the stranger had written there. Type one
        character into a description that looks empty, open your own history,
        read the concealed value back.
        """
        baker.make(
            "dashboard.WikiEdit",
            wiki=self.wiki,
            editor=self.viewer_user.profile,
            changes={"description": {"from": "CANARY-HIDDEN-PRIOR", "to": "x"}},
            reverted=False,
        )

        body = self._history()

        self.assertNotIn("CANARY-HIDDEN-PRIOR", body)


class ConcealedPanelTests(TestCase):
    """The HTMX panels, which the page-level test never touches.

    Aliases, comments and links load from their own endpoints after the page
    renders, so a canary planted for the main view is never in that response.
    Those assertions were decorative until these tests existed.
    """

    def setUp(self) -> None:
        super().setUp()
        self.location = baker.make(Location, latitude=39.7392, longitude=-104.9903, official_name="Provider Name")
        self.wiki = baker.make(Wiki, location=self.location, name="Provider Name")
        self.stranger = baker.make(User).profile
        self.viewer_user = baker.make(User)
        baker.make(Pin, profile=self.viewer_user.profile, location=self.location, parent_pin=None)
        self.slug = self.location.slug or str(self.location.uuid)

    def _get(self, route: str) -> str:
        self.client.force_login(self.viewer_user)
        with mock.patch("urbanlens.dashboard.services.wiki.concealment.concealment_active", return_value=True):
            response = self.client.get(reverse(route, kwargs={"location_slug": self.slug}))
        self.assertEqual(response.status_code, 200)
        return response.content.decode()

    def test_the_alias_panel_omits_a_strangers_alias(self) -> None:
        """Aliases are how a concealed name comes back as a row."""
        from urbanlens.dashboard.models.aliases.model import AliasSource, WikiAlias

        baker.make(WikiAlias, wiki=self.wiki, name="CANARY-ALIAS", source=AliasSource.USER, created_by=self.stranger)

        self.assertNotIn("CANARY-ALIAS", self._get("location.wiki.aliases"))

    def test_the_comment_panel_omits_a_strangers_comment(self) -> None:
        """Comments are where people write down how they got in.

        The stranger needs a pin here, and that is not incidental. Their
        ``comment_visibility`` defaults to ANYTHING_IN_COMMON, so without a
        shared pin the *settings* gate hides the comment and concealment is
        never reached - the assertion passes while proving nothing. Verified by
        disabling concealment: with no pin the test still passed, with a pin it
        fails as it should.
        """
        baker.make(Pin, profile=self.stranger, location=self.location, parent_pin=None)
        baker.make("dashboard.Comment", wiki=self.wiki, pin=None, profile=self.stranger, text="CANARY-COMMENT")

        self.assertNotIn("CANARY-COMMENT", self._get("location.wiki.comments"))

    def test_the_links_row_omits_a_strangers_link(self) -> None:
        """A link somebody added is a contribution like any other."""
        baker.make(
            "dashboard.WikiLink",
            wiki=self.wiki,
            name="CANARY-LINK",
            url="https://example.invalid/x",
            created_by=self.stranger,
        )

        self.assertNotIn("CANARY-LINK", self._get("location.wiki.links"))


class ConcealedMediaTests(TestCase):
    """Photos, layers and overlays - the surfaces that show where people stood."""

    def setUp(self) -> None:
        super().setUp()
        self.location = baker.make(Location, latitude=47.6062, longitude=-122.3321, official_name="Provider Name")
        self.wiki = baker.make(Wiki, location=self.location, name="Provider Name")
        self.stranger = baker.make(User).profile
        self.viewer_user = baker.make(User)
        baker.make(Pin, profile=self.viewer_user.profile, location=self.location, parent_pin=None)
        # The stranger needs a pin too. photo_upload_visibility defaults to
        # ANYTHING_IN_COMMON, so without one visible_to drops their upload
        # before conceal_rows is reached and the assertion below passes while
        # testing nothing - the same defect the comment test had.
        baker.make(Pin, profile=self.stranger, location=self.location, parent_pin=None)
        self.slug = self.location.slug or str(self.location.uuid)

    def _get(self, route: str) -> str:
        self.client.force_login(self.viewer_user)
        with mock.patch("urbanlens.dashboard.services.wiki.concealment.concealment_active", return_value=True):
            response = self.client.get(reverse(route, kwargs={"location_slug": self.slug}))
        self.assertEqual(response.status_code, 200)
        return response.content.decode()

    def test_the_photo_map_layer_omits_a_strangers_upload(self) -> None:
        """This layer plots photos at their capture coordinates.

        An unfiltered payload does not merely list other people's
        contributions - it maps where they stood.
        """
        from urbanlens.dashboard.models.images.model import Image, ImageSource, MediaKind

        baker.make(
            Image,
            wiki=self.wiki,
            profile=self.stranger,
            source=ImageSource.UPLOAD,
            media_type=MediaKind.PHOTO,
            image="pin_images/CANARY-UPLOAD.png",
            latitude=47.6062,
            longitude=-122.3321,
        )

        self.assertNotIn("CANARY-UPLOAD", self._get("location.wiki.gallery.json"))

    def test_concealment_keeps_provider_media_the_viewer_can_already_see(self) -> None:
        """Positive control, scoped to what this layer is actually responsible for.

        An earlier version attributed the provider row to a stranger and
        expected it through, which the app does not do - and the reason is
        worth keeping. ``Image.profile`` on a materialised provider row is the
        *up-voter*, not the photographer, so ``visible_to`` applies that
        voter's photo settings and drops the row before concealment is reached.

        Widening ``visible_to`` to admit those rows is a privacy-model decision
        affecting every viewer, and the concealment spec says explicitly not to
        make it as part of this work. So this asserts the thing concealment
        owns: a non-UPLOAD row the viewer can already see survives the filter.
        """
        from urbanlens.dashboard.models.images.model import Image, ImageSource, MediaKind

        baker.make(
            Image,
            wiki=self.wiki,
            profile=self.stranger,
            source=ImageSource.WIKIMEDIA,
            media_type=MediaKind.PHOTO,
            image="pin_images/PROVIDER-OK.png",
            latitude=47.6062,
            longitude=-122.3321,
        )

        self.assertIn("PROVIDER-OK", self._get("location.wiki.gallery.json"))

    def test_the_page_carries_no_custom_layers(self) -> None:
        """A layer of entrance routes and camera markers is the sharpest tell there is."""
        from urbanlens.dashboard.models.markup.model import CustomLayer

        baker.make(CustomLayer, parent_wiki=self.wiki, name="CANARY-LAYER", profile=self.stranger)

        # Through _get, which asserts 200 - inlining the request meant a 302 to
        # login, a 404 or a 500 all satisfied the assertion.
        self.assertNotIn("CANARY-LAYER", self._get("location.wiki"))


class FriendVisibilityTests(TestCase):
    """Rule 5 at render level: a friend's contribution must still arrive.

    Every other assertion in this file is negative, so a bug that concealed too
    much would be invisible to the whole suite. That is not hypothetical - both
    the alias and link panels re-derive the viewer as
    ``getattr(request.user, "profile", None)`` rather than using the profile
    ``resolve_visible_wiki`` already returned, and if that ever yields None the
    viewer silently loses their own and their friends' rows with every test
    still green.
    """

    def setUp(self) -> None:
        super().setUp()
        self.location = baker.make(Location, latitude=42.3601, longitude=-71.0589, official_name="Provider Name")
        self.wiki = baker.make(Wiki, location=self.location, name="Provider Name")
        self.viewer_user = baker.make(User)
        self.friend = baker.make(User).profile
        baker.make(Pin, profile=self.viewer_user.profile, location=self.location, parent_pin=None)
        baker.make(Pin, profile=self.friend, location=self.location, parent_pin=None)
        baker.make(
            "dashboard.Friendship",
            from_profile=self.viewer_user.profile,
            to_profile=self.friend,
            status="Accepted",
        )
        self.slug = self.location.slug or str(self.location.uuid)

    def _get(self, route: str) -> str:
        self.client.force_login(self.viewer_user)
        with mock.patch("urbanlens.dashboard.services.wiki.concealment.concealment_active", return_value=True):
            response = self.client.get(reverse(route, kwargs={"location_slug": self.slug}))
        self.assertEqual(response.status_code, 200)
        return response.content.decode()

    def test_a_friends_alias_still_reaches_the_panel(self) -> None:
        """ "I put a load of stuff on the wiki" has to remain true."""
        from urbanlens.dashboard.models.aliases.model import AliasSource, WikiAlias

        baker.make(WikiAlias, wiki=self.wiki, name="FRIEND-ALIAS", source=AliasSource.USER, created_by=self.friend)

        self.assertIn("FRIEND-ALIAS", self._get("location.wiki.aliases"))

    def test_a_friends_comment_still_reaches_the_panel(self) -> None:
        """The case the friend clause exists for."""
        baker.make("dashboard.Comment", wiki=self.wiki, pin=None, profile=self.friend, text="FRIEND-COMMENT")

        self.assertIn("FRIEND-COMMENT", self._get("location.wiki.comments"))

    def test_a_friends_field_edit_still_resolves(self) -> None:
        """Field values, not just rows."""
        from urbanlens.dashboard.services.wiki.concealment import concealed_field_values

        with writing_as(WriteSource.USER, actor=self.friend.pk):
            Wiki.objects.filter(pk=self.wiki.pk).update(description="FRIEND-WROTE-THIS")

        values = concealed_field_values(Wiki.objects.get(pk=self.wiki.pk), self.viewer_user.profile)

        self.assertEqual(values["description"], "FRIEND-WROTE-THIS")
