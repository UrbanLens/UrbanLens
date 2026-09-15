"""A viewer's wiki reach must reach SQL as a subquery, not as a list of every place they have pinned.

Measured on the capacity population (X18): a 17,720-pin account's global search spent 95-176 ms planning each
provider's query against about 1 ms executing it, and 110-200 ms more in Django and the driver, because every
pinned location id travelled as a parameter. Nothing about the answer depends on shipping those ids.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from django.contrib.auth.models import User
from django.db import connection
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.article.model import Article
from urbanlens.dashboard.models.friendship.model import Friendship, FriendshipStatus
from urbanlens.dashboard.models.images.model import Image
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.models.wiki.model import Wiki
from urbanlens.dashboard.services.custom_fields.custom_field_references import referenceable_queryset
from urbanlens.dashboard.services.global_search import GlobalSearchEngine
from urbanlens.dashboard.services.map_pins.autocomplete import search_local
from urbanlens.dashboard.services.profile.profile_photos import strip_photos_visible_to

FEW_PINS = 2
MORE_PINS = 12


def _widest_statement(call: Callable[[], Any]) -> tuple[int, int]:
    """The most parameters any one statement of *call* carried, and how many statements ran."""
    widths: list[int] = []

    def wrapper(execute: Any, sql: str, params: Any, many: bool, context: dict[str, Any]) -> Any:
        widths.append(sum(len(param) if isinstance(param, list | tuple | set) else 1 for param in params or ()))
        return execute(sql, params, many, context)

    with connection.execute_wrapper(wrapper):
        call()
    return max(widths, default=0), len(widths)


class WikiReachDoesNotGrowWithPinsTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # the first user is auto-promoted to site admin
        self.viewer = baker.make(User).profile
        self.uploader = baker.make(User).profile
        Friendship.objects.create(from_profile=self.viewer, to_profile=self.uploader, status=FriendshipStatus.ACCEPTED)
        self.pinned = 0
        self._pin(FEW_PINS)
        wiki = baker.make(Wiki, location=Pin.objects.filter(profile=self.viewer).first().location, name="Reach Wiki")
        baker.make(Article, wiki=wiki, content="Reach article")
        baker.make(Image, profile=self.uploader, wiki=wiki, pending_scan=False, caption="Reach photo")

    def _pin(self, count: int) -> None:
        for _ in range(count):
            self.pinned += 1
            location = Location.objects.create(latitude=10 + self.pinned * 0.01, longitude=20 + self.pinned * 0.01)
            baker.make(Pin, profile=self.viewer, location=location)

    def _fresh_viewer(self) -> Profile:
        # Reach is memoised on the instance, so a reused one would measure the first reading twice.
        return Profile.objects.get(pk=self.viewer.pk)

    def assertDoesNotGrow(self, call: Callable[[Profile], Any]) -> None:
        few, statements = _widest_statement(lambda: call(self._fresh_viewer()))
        self.assertGreater(statements, 0, "nothing ran, so nothing was measured")
        self._pin(MORE_PINS - FEW_PINS)
        more, _ = _widest_statement(lambda: call(self._fresh_viewer()))
        self.assertEqual(
            more, few, f"a statement grew from {few} to {more} parameters with {MORE_PINS - FEW_PINS} more pins"
        )

    def test_global_search(self) -> None:
        self.assertDoesNotGrow(lambda viewer: GlobalSearchEngine().search(viewer, "Reach"))

    def test_articles_a_viewer_may_read(self) -> None:
        self.assertDoesNotGrow(lambda viewer: list(Article.objects.visible_to(viewer)))

    def test_photos_a_viewer_may_see(self) -> None:
        self.assertDoesNotGrow(lambda viewer: list(Image.objects.all().visible_to(viewer)))

    def test_map_autocomplete(self) -> None:
        self.assertDoesNotGrow(lambda viewer: search_local("Reach", viewer))

    def test_wikis_a_custom_field_may_reference(self) -> None:
        self.assertDoesNotGrow(lambda viewer: list(referenceable_queryset("wiki", viewer)))

    def test_a_profile_photo_strip(self) -> None:
        self.assertDoesNotGrow(lambda viewer: list(strip_photos_visible_to(self.uploader, viewer)))
