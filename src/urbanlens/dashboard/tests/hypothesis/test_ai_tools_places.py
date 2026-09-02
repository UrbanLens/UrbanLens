"""Tests for services.ai.tools.places - has_tunnels, through registry.execute()."""

from __future__ import annotations

from datetime import UTC, datetime

from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.baker_recipes import _make_profile
from urbanlens.dashboard.models.comments.model import Comment
from urbanlens.dashboard.models.floorplans.model import Floorplan
from urbanlens.dashboard.models.images.model import Image
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.place.model import Place
from urbanlens.dashboard.models.wiki.model import Wiki
from urbanlens.dashboard.services.ai.tools.registry import ToolContext, available_tools, execute

_LAT, _LNG = "42.5", "-73.5"


def _plain_profile():
    """A profile with SiteFeature.AI granted - see test_ai_tools_registry.py's own docstring for why."""
    from urbanlens.dashboard.models.site_settings.model import SiteSettings
    from urbanlens.dashboard.models.subscriptions import SiteFeature

    baker.make("auth.User")
    settings_obj = SiteSettings.get_current()
    SiteSettings.objects.filter(pk=settings_obj.pk).update(default_features=SiteFeature.AI)
    return _make_profile()


def _context(profile) -> ToolContext:
    return ToolContext(profile=profile, now=datetime.now(tz=UTC))


class HasTunnelsTests(TestCase):
    def setUp(self) -> None:
        self.profile = _plain_profile()
        self.other = _plain_profile()
        self.place = baker.make(Place)
        self.location = baker.make(Location, latitude=_LAT, longitude=_LNG, place=self.place)
        self.pin = baker.make(Pin, profile=self.profile, location=self.location, name="Mine", name_is_user_provided=True)

    def test_appears_in_available_tools(self) -> None:
        names = {spec.name for spec in available_tools(_context(self.profile))}
        self.assertIn("has_tunnels", names)

    def test_no_evidence_when_nothing_matches(self) -> None:
        result = execute("has_tunnels", {"pin_slug": self.pin.slug}, _context(self.profile))
        self.assertEqual(result.data["verdict"], "no_evidence")
        self.assertEqual(result.data["sources"], [])

    def test_a_personal_floorplan_with_a_below_grade_floor_is_evidence(self) -> None:
        floorplan = baker.make(Floorplan, place=self.place, profile=self.profile, wiki=None)
        floorplan.floors.create(level=-1)
        floorplan.floors.create(level=0)

        result = execute("has_tunnels", {"pin_slug": self.pin.slug}, _context(self.profile))

        self.assertEqual(result.data["verdict"], "evidence")
        self.assertIn("floorplan", result.data["sources"])
        self.assertIn("1 level(s)", result.data["floorplan_note"])

    def test_another_profiles_personal_floorplan_is_never_evidence(self) -> None:
        """A private plan someone else drew for the same building must not leak."""
        theirs = baker.make(Floorplan, place=self.place, profile=self.other, wiki=None)
        theirs.floors.create(level=-2)

        result = execute("has_tunnels", {"pin_slug": self.pin.slug}, _context(self.profile))

        self.assertEqual(result.data["verdict"], "no_evidence")

    def test_a_published_community_floorplan_is_evidence_when_the_wiki_is_visible(self) -> None:
        wiki = baker.make(Wiki, location=self.location, place=self.place)
        published = baker.make(Floorplan, place=self.place, profile=self.other, wiki=wiki)
        published.floors.create(level=-1)

        result = execute("has_tunnels", {"pin_slug": self.pin.slug}, _context(self.profile))

        self.assertEqual(result.data["verdict"], "evidence")
        self.assertIn("floorplan", result.data["sources"])

    def test_own_uploaded_photo_caption_is_evidence(self) -> None:
        baker.make(Image, image="pin_images/tunnel.png", profile=self.profile, location=self.location, caption="Found a tunnel entrance here")

        result = execute("has_tunnels", {"pin_slug": self.pin.slug}, _context(self.profile))

        self.assertEqual(result.data["verdict"], "evidence")
        self.assertIn("images", result.data["sources"])
        self.assertIn("tunnel", result.data["image_captions"][0]["caption"].lower())

    def test_another_profiles_unshared_photo_is_never_evidence(self) -> None:
        """A photo nobody shared into a wiki this profile can reach must not leak."""
        baker.make(Image, image="pin_images/secret.png", profile=self.other, location=self.location, caption="secret tunnel network")

        result = execute("has_tunnels", {"pin_slug": self.pin.slug}, _context(self.profile))

        self.assertEqual(result.data["verdict"], "no_evidence")

    def test_a_wiki_comment_mentioning_tunnels_is_evidence(self) -> None:
        wiki = baker.make(Wiki, location=self.location, place=self.place)
        baker.make(Comment, wiki=wiki, pin=None, profile=self.profile, text="There's a tunnel under the east wing.")

        result = execute("has_tunnels", {"pin_slug": self.pin.slug}, _context(self.profile))

        self.assertEqual(result.data["verdict"], "evidence")
        self.assertIn("comments", result.data["sources"])
        self.assertIn("tunnel", result.data["comment_snippets"][0]["snippet"].lower())

    def test_missing_pin_slug_is_an_error_block_not_a_raise(self) -> None:
        result = execute("has_tunnels", {"pin_slug": ""}, _context(self.profile))
        self.assertIn("error", result.data)

    def test_another_profiles_pin_slug_never_resolves(self) -> None:
        theirs = baker.make(Pin, profile=self.other, location=baker.make(Location, latitude="44.0", longitude="-75.0"), name="Theirs", name_is_user_provided=True)
        result = execute("has_tunnels", {"pin_slug": theirs.slug}, _context(self.profile))
        self.assertIn("error", result.data)
