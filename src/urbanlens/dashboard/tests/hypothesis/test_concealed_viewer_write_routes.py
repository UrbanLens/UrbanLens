"""The community wiki write routes P29 covered, for a viewer the wiki is concealed from.

``concealment_active`` returns ``False`` in production until the gate is defined
(``docs/designs/concealed-wiki-spec.md`` §0.1), so these force it on the way the other concealment suites do: by
patching that one predicate. ``external_api.views_wiki`` imports it by name, so its binding is patched as well.

A concealed viewer is handed a projection that refuses to save. Each write must land on the real row, leave what
a stranger wrote there intact, and answer without showing it.
"""

from __future__ import annotations

from unittest import mock

from django.urls import reverse
from model_bakery import baker

from urbanlens.dashboard.models.abstract.choices import SecurityLevel
from urbanlens.dashboard.models.abstract.versioning import WriteSource, writing_as
from urbanlens.dashboard.models.account.model import ApiKeyScope
from urbanlens.dashboard.models.aliases.model import AliasSource, AliasType, WikiAlias
from urbanlens.dashboard.models.images.model import Image
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.wiki.model import Wiki
from urbanlens.dashboard.models.wiki.revision import WikiFieldRevision
from urbanlens.dashboard.models.wiki_edit import WikiEdit
from urbanlens.dashboard.models.wiki_stat_vote.model import WikiStatVote
from urbanlens.dashboard.services.wiki.wiki_edits import apply_wiki_edit
from urbanlens.dashboard.tests.hypothesis.external_api_helpers import ExternalApiRouteCase

SECRET = "A stranger's notes on the loose panel"
_CONCEALMENT_PREDICATES = (
    "urbanlens.dashboard.services.wiki.concealment.concealment_active",
    "urbanlens.dashboard.external_api.views_wiki.concealment_active",
)


class _ConcealedWikiFixture(ExternalApiRouteCase):
    """A wiki the owner has pinned, which a stranger has written up, with concealment on for every viewer."""

    scopes = (ApiKeyScope.WIKI_READ, ApiKeyScope.WIKI_WRITE)
    read_scopes = (ApiKeyScope.WIKI_READ,)

    def setUp(self) -> None:
        super().setUp()
        self.location = baker.make(Location)
        self.wiki = baker.make(Wiki, location=self.location)
        WikiFieldRevision.objects.filter(target=self.wiki).delete()
        with writing_as(WriteSource.AUTOMATIC):
            Wiki.objects.filter(pk=self.wiki.pk).update(name="Provider Name")
        baker.make(Pin, profile=self.owner, location=self.location)
        baker.make(Pin, profile=self.stranger, location=self.location)
        self.stranger_edit = apply_wiki_edit(
            self._wiki(), self.stranger, {"description": SECRET, "cameras": SecurityLevel.EVERYWHERE}
        )
        self.slug = self.location.ensure_slug()
        for target in _CONCEALMENT_PREDICATES:
            patcher = mock.patch(target, return_value=True)
            patcher.start()
            self.addCleanup(patcher.stop)

    def _wiki(self) -> Wiki:
        return Wiki.objects.get(pk=self.wiki.pk)

    def assert_strangers_writing_kept(self) -> None:
        wiki = self._wiki()
        self.assertEqual((wiki.description, wiki.cameras), (SECRET, SecurityLevel.EVERYWHERE))


class ConcealmentIsOnTests(_ConcealedWikiFixture):
    """The control: without these the rest would pass with the gate off."""

    def test_the_detail_hides_the_strangers_writing_while_patched(self) -> None:
        response = self.client.get(reverse("external_api:wikis.detail", args=[self.slug]), **self.auth)

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, SECRET)
        self.assertEqual(response.json()["name"], "Provider Name")

    def test_the_detail_shows_it_once_unpatched(self) -> None:
        with (
            mock.patch(_CONCEALMENT_PREDICATES[0], return_value=False),
            mock.patch(_CONCEALMENT_PREDICATES[1], return_value=False),
        ):
            response = self.client.get(reverse("external_api:wikis.detail", args=[self.slug]), **self.auth)

        self.assertContains(response, SECRET)


class ConcealedWikiDetailEditTests(_ConcealedWikiFixture):
    """``external_api:wikis.detail`` PATCH and the dashboard's ``location.wiki.edit``."""

    def test_an_api_edit_lands_on_the_real_row_and_answers_concealed(self) -> None:
        response = self.send("patch", reverse("external_api:wikis.detail", args=[self.slug]), {"name": "Mill No. 2"})

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(self._wiki().name, "Mill No. 2")
        self.assert_strangers_writing_kept()
        self.assertNotContains(response, SECRET)
        self.assertEqual(response.json()["name"], "Mill No. 2")

    def test_a_dashboard_edit_lands_on_the_real_row_and_answers_concealed(self) -> None:
        self.client.force_login(self.owner_user)

        response = self.client.post(reverse("location.wiki.edit", args=[self.slug]), {"name": "Mill No. 2"})

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(self._wiki().name, "Mill No. 2")
        self.assert_strangers_writing_kept()
        self.assertNotIn(SECRET, response.json()["about_html"])


class ConcealedRevertTests(_ConcealedWikiFixture):
    """``external_api:wikis.history.revert`` only reaches edits the viewer may see."""

    def test_a_strangers_edit_is_404_and_stays(self) -> None:
        response = self.send(
            "post", reverse("external_api:wikis.history.revert", args=[self.slug, self.stranger_edit.pk])
        )

        self.assertEqual(response.status_code, 404)
        self.assertFalse(WikiEdit.objects.get(pk=self.stranger_edit.pk).reverted)
        self.assert_strangers_writing_kept()

    def test_the_viewers_own_edit_is_reverted_on_the_real_row(self) -> None:
        own_edit = apply_wiki_edit(self._wiki(), self.owner, {"name": "Mill No. 2"})

        response = self.send("post", reverse("external_api:wikis.history.revert", args=[self.slug, own_edit.pk]))

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(self._wiki().name, "Provider Name")
        self.assert_strangers_writing_kept()
        self.assertNotContains(response, SECRET)


class ConcealedStatVoteTests(_ConcealedWikiFixture):
    """``external_api:wikis.votes`` and ``location.wiki.stat_vote`` answer with the viewer's own ballot alone."""

    def setUp(self) -> None:
        super().setUp()
        WikiStatVote.objects.cast(self.wiki, self.stranger, "danger", 5)

    def test_an_api_vote_is_stored_and_reports_only_the_viewers_ballot(self) -> None:
        response = self.send("put", reverse("external_api:wikis.votes", args=[self.slug, "danger"]), {"value": 1})

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["exact"], 1.0)
        self.assertEqual(response.json()["my_vote"], 1)
        self.assertEqual(WikiStatVote.objects.filter(wiki=self.wiki, field="danger").count(), 2)

    def test_a_dashboard_vote_is_stored_and_reports_only_the_viewers_ballot(self) -> None:
        self.client.force_login(self.owner_user)

        response = self.client.post(reverse("location.wiki.stat_vote", args=[self.slug, "danger"]), {"value": "1"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["composite"].exact, 1.0)
        self.assertEqual(WikiStatVote.objects.filter(wiki=self.wiki, field="danger").count(), 2)


class ConcealedAliasTests(_ConcealedWikiFixture):
    """``external_api:wikis.aliases`` and ``wikis.aliases.use`` write through to the real row."""

    def setUp(self) -> None:
        super().setUp()
        self.strangers_alias = WikiAlias.objects.create(
            wiki=self.wiki,
            name="The Panel Place",
            kind=AliasType.ALTERNATE,
            source=AliasSource.USER,
            created_by=self.stranger,
        )

    def test_an_added_name_lands_on_the_real_wiki(self) -> None:
        response = self.send("post", reverse("external_api:wikis.aliases", args=[self.slug]), {"name": "Brick Mill"})

        self.assertEqual(response.status_code, 201, response.content)
        self.assertTrue(WikiAlias.objects.filter(wiki=self.wiki, name="Brick Mill").exists())
        self.assert_strangers_writing_kept()

    def test_a_strangers_alias_cannot_be_promoted(self) -> None:
        response = self.send(
            "post", reverse("external_api:wikis.aliases.use", args=[self.slug, self.strangers_alias.pk])
        )

        self.assertEqual(response.status_code, 404)
        self.assertEqual(self._wiki().name, "Provider Name")

    def test_the_viewers_own_alias_renames_the_real_row(self) -> None:
        self.send("post", reverse("external_api:wikis.aliases", args=[self.slug]), {"name": "Brick Mill"})
        own = WikiAlias.objects.get(wiki=self.wiki, name="Brick Mill")

        response = self.send("post", reverse("external_api:wikis.aliases.use", args=[self.slug, own.pk]))

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(self._wiki().name, "Brick Mill")
        self.assert_strangers_writing_kept()
        self.assertNotContains(response, SECRET)


class ConcealedCoverPhotoTests(_ConcealedWikiFixture):
    """``external_api:wikis.cover_photo`` sets and clears the real row's cover."""

    def test_setting_and_clearing_the_cover(self) -> None:
        image = baker.make(Image, wiki=self.wiki, location=self.location, profile=self.owner, image="photos/own.jpg")
        url = reverse("external_api:wikis.cover_photo", args=[self.slug])

        response = self.send("put", url, {"image_uuid": str(image.uuid)})

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(self._wiki().cover_photo_id, image.pk)
        self.assert_strangers_writing_kept()

        self.assertEqual(self.send("delete", url).status_code, 200)
        self.assertIsNone(self._wiki().cover_photo_id)
        self.assert_strangers_writing_kept()
