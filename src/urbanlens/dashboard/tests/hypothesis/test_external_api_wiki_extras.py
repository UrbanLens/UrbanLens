"""Tests for the wiki extras added in the P2 parity pass: boundary, cover photo,
alias nickname toggle, ownership/sale history, and article-revision hard-delete.

Every endpoint here inherits the same anti-enumeration invariant as the rest
of the wiki surface (``services.wiki.wiki_access.resolve_visible_wiki``): a wiki
the caller has not earned access to is a 404, never a 403. That exhaustive
property is covered once, for the whole surface, by
``test_external_api_wiki_oracle.py``; these tests check each endpoint's own
behavior instead of re-proving the shared gate.
"""

from __future__ import annotations

from uuid import uuid4

from django.contrib.auth.models import User
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.account.model import ApiKey, ApiKeyScope
from urbanlens.dashboard.models.aliases.model import AliasType, WikiAlias
from urbanlens.dashboard.models.article.model import ArticleRevision
from urbanlens.dashboard.models.images.model import Image
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.models.property_owner.model import WikiOwner, WikiPropertySale
from urbanlens.dashboard.models.wiki_edit import WikiEdit
from urbanlens.dashboard.services.auth.api_keys import generate_api_key
from urbanlens.dashboard.services.wiki.articles import get_article, restore_revision, save_article
from urbanlens.dashboard.tests.hypothesis.test_external_api_wiki_oracle import disable_throttling, grant_wiki_scopes

BASE = "/dashboard/api/external/v1/wikis"

_SQUARE_GEOJSON = {
    "type": "Polygon",
    "coordinates": [[[-105.001, 40.001], [-105.001, 40.002], [-105.002, 40.002], [-105.002, 40.001], [-105.001, 40.001]]],
}


class _WikiExtrasTestCase(TestCase):
    """Shared fixture: one wiki the caller has pinned, plus a wiki-scoped key."""

    def setUp(self) -> None:
        baker.make(User)  # first user auto-promoted to bootstrap site admin
        self.user = baker.make(User)
        self.profile = Profile.objects.get(user=self.user)
        _key, self.raw_key = generate_api_key(self.user, "Wiki client")
        grant_wiki_scopes(self.user)
        disable_throttling(self)

        self.location = baker.make("dashboard.Location", latitude=40.0015, longitude=-105.0015)
        self.wiki = baker.make("dashboard.Wiki", location=self.location, name="Old Mill")
        baker.make("dashboard.Pin", profile=self.profile, location=self.location)

    def headers(self) -> dict:
        """Request kwargs carrying the fixture's API key as a bearer token."""
        return {"HTTP_AUTHORIZATION": f"Bearer {self.raw_key}"}

    def url(self, suffix: str = "") -> str:
        """A URL under the fixture wiki."""
        return f"{BASE}/{self.location.ensure_slug()}/{suffix}"


class WikiAliasToggleNicknameTests(_WikiExtrasTestCase):
    """POST .../aliases/{id}/toggle-nickname/."""

    def test_toggle_flips_alternate_to_nickname(self) -> None:
        alias = baker.make(WikiAlias, wiki=self.wiki, name="Mill Ruins", kind=AliasType.ALTERNATE)
        response = self.client.post(self.url(f"aliases/{alias.pk}/toggle-nickname/"), **self.headers())

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["kind"], AliasType.NICKNAME)
        alias.refresh_from_db()
        self.assertEqual(alias.kind, AliasType.NICKNAME)

    def test_toggle_is_its_own_inverse(self) -> None:
        alias = baker.make(WikiAlias, wiki=self.wiki, name="Mill Ruins", kind=AliasType.NICKNAME)
        self.client.post(self.url(f"aliases/{alias.pk}/toggle-nickname/"), **self.headers())
        alias.refresh_from_db()
        self.assertEqual(alias.kind, AliasType.ALTERNATE)

    def test_toggle_records_no_edit_history(self) -> None:
        """Unlike alias-use, this is a display preference, not a rename."""
        alias = baker.make(WikiAlias, wiki=self.wiki, name="Mill Ruins", kind=AliasType.ALTERNATE)
        before = WikiEdit.objects.filter(wiki=self.wiki).count()
        self.client.post(self.url(f"aliases/{alias.pk}/toggle-nickname/"), **self.headers())
        self.assertEqual(WikiEdit.objects.filter(wiki=self.wiki).count(), before)

    def test_an_alias_from_another_wiki_is_not_found(self) -> None:
        other_wiki = baker.make("dashboard.Wiki", location=baker.make("dashboard.Location"), name="Elsewhere")
        foreign_alias = baker.make(WikiAlias, wiki=other_wiki, name="Their Name")
        response = self.client.post(self.url(f"aliases/{foreign_alias.pk}/toggle-nickname/"), **self.headers())
        self.assertEqual(response.status_code, 404)

    def test_requires_wiki_write(self) -> None:
        ApiKey.objects.filter(user=self.user).update(scopes=[ApiKeyScope.WIKI_READ.value])
        alias = baker.make(WikiAlias, wiki=self.wiki, name="Mill Ruins")
        response = self.client.post(self.url(f"aliases/{alias.pk}/toggle-nickname/"), **self.headers())
        self.assertEqual(response.status_code, 403)


class WikiBoundaryTests(_WikiExtrasTestCase):
    """GET/POST .../boundary/."""

    def test_get_returns_the_location_coordinates_and_both_typed_slots(self) -> None:
        response = self.client.get(self.url("boundary/"), **self.headers())
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertAlmostEqual(payload["latitude"], 40.0015, places=3)
        self.assertAlmostEqual(payload["longitude"], -105.0015, places=3)
        self.assertIn("property", payload["boundaries"])
        self.assertIn("building", payload["boundaries"])

    def test_post_saves_a_custom_polygon_and_records_an_edit(self) -> None:
        response = self.client.post(
            self.url("boundary/"), {"boundary_type": "property", "polygon": _SQUARE_GEOJSON}, content_type="application/json", **self.headers()
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["boundaries"]["property"]["source"], "wiki")

        edit = WikiEdit.objects.filter(wiki=self.wiki).latest("created")
        self.assertIn("boundary_property", edit.changes)
        self.assertEqual(edit.editor, self.profile)

    def test_post_with_null_polygon_clears_a_previously_saved_one(self) -> None:
        self.client.post(self.url("boundary/"), {"boundary_type": "property", "polygon": _SQUARE_GEOJSON}, content_type="application/json", **self.headers())
        response = self.client.post(self.url("boundary/"), {"boundary_type": "property", "polygon": None}, content_type="application/json", **self.headers())

        self.assertEqual(response.status_code, 200, response.content)
        self.assertNotEqual(response.json()["boundaries"]["property"]["source"], "wiki")

    def test_post_rejects_an_invalid_boundary_type(self) -> None:
        response = self.client.post(self.url("boundary/"), {"boundary_type": "county", "polygon": None}, content_type="application/json", **self.headers())
        self.assertEqual(response.status_code, 400)

    def test_post_rejects_malformed_geometry(self) -> None:
        response = self.client.post(
            self.url("boundary/"), {"boundary_type": "property", "polygon": {"type": "Point", "coordinates": [1, 2]}}, content_type="application/json", **self.headers()
        )
        self.assertEqual(response.status_code, 400)

    def test_unpinned_location_is_not_found(self) -> None:
        other_location = baker.make("dashboard.Location")
        baker.make("dashboard.Wiki", location=other_location)
        response = self.client.get(f"{BASE}/{other_location.ensure_slug()}/boundary/", **self.headers())
        self.assertEqual(response.status_code, 404)

    def test_post_requires_wiki_write(self) -> None:
        ApiKey.objects.filter(user=self.user).update(scopes=[ApiKeyScope.WIKI_READ.value])
        response = self.client.post(self.url("boundary/"), {"boundary_type": "property", "polygon": None}, content_type="application/json", **self.headers())
        self.assertEqual(response.status_code, 403)


class WikiCoverPhotoTests(_WikiExtrasTestCase):
    """PUT/DELETE .../cover-photo/."""

    def test_put_sets_the_cover_photo_from_the_wikis_gallery(self) -> None:
        image = baker.make(Image, profile=self.profile, wiki=self.wiki)
        response = self.client.put(self.url("cover-photo/"), {"image_uuid": str(image.uuid)}, content_type="application/json", **self.headers())

        self.assertEqual(response.status_code, 200, response.content)
        self.wiki.refresh_from_db()
        self.assertEqual(self.wiki.cover_photo_id, image.pk)

    def test_put_rejects_an_image_not_in_this_wikis_gallery(self) -> None:
        other_wiki = baker.make("dashboard.Wiki", location=baker.make("dashboard.Location"))
        image = baker.make(Image, profile=self.profile, wiki=other_wiki)
        response = self.client.put(self.url("cover-photo/"), {"image_uuid": str(image.uuid)}, content_type="application/json", **self.headers())

        self.assertEqual(response.status_code, 404)
        self.wiki.refresh_from_db()
        self.assertIsNone(self.wiki.cover_photo_id)

    def test_put_rejects_an_unknown_image_uuid(self) -> None:
        response = self.client.put(self.url("cover-photo/"), {"image_uuid": str(uuid4())}, content_type="application/json", **self.headers())
        self.assertEqual(response.status_code, 404)

    def test_delete_clears_the_cover_photo(self) -> None:
        image = baker.make(Image, profile=self.profile, wiki=self.wiki)
        self.wiki.cover_photo = image
        self.wiki.save(update_fields=["cover_photo"])

        response = self.client.delete(self.url("cover-photo/"), **self.headers())

        self.assertEqual(response.status_code, 200, response.content)
        self.assertIsNone(response.json()["cover_photo_url"])
        self.wiki.refresh_from_db()
        self.assertIsNone(self.wiki.cover_photo_id)

    def test_put_requires_wiki_write(self) -> None:
        ApiKey.objects.filter(user=self.user).update(scopes=[ApiKeyScope.WIKI_READ.value])
        image = baker.make(Image, profile=self.profile, wiki=self.wiki)
        response = self.client.put(self.url("cover-photo/"), {"image_uuid": str(image.uuid)}, content_type="application/json", **self.headers())
        self.assertEqual(response.status_code, 403)


class WikiOwnershipTests(_WikiExtrasTestCase):
    """GET .../ownership/."""

    def test_lists_owners_linked_to_this_location(self) -> None:
        owner = baker.make(WikiOwner, name="Alice Smith")
        owner.locations.add(self.location)

        response = self.client.get(self.url("ownership/"), **self.headers())

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["count"], 1)
        self.assertEqual(payload["results"][0]["name"], "Alice Smith")

    def test_excludes_owners_of_other_locations(self) -> None:
        owner = baker.make(WikiOwner, name="Bob Jones")
        owner.locations.add(baker.make("dashboard.Location"))

        response = self.client.get(self.url("ownership/"), **self.headers())
        self.assertEqual(response.json()["count"], 0)

    def test_requires_wiki_read(self) -> None:
        ApiKey.objects.filter(user=self.user).update(scopes=[])
        response = self.client.get(self.url("ownership/"), **self.headers())
        self.assertEqual(response.status_code, 403)


class WikiPropertySalesTests(_WikiExtrasTestCase):
    """GET .../sales/."""

    def test_lists_sales_with_nested_owners(self) -> None:
        seller = baker.make(WikiOwner, name="Old Owner LLC")
        buyer = baker.make(WikiOwner, name="New Owner LLC")
        sale = baker.make(WikiPropertySale, location=self.location, sale_price="150000.00")
        sale.previous_owners.add(seller)
        sale.new_owners.add(buyer)

        response = self.client.get(self.url("sales/"), **self.headers())

        self.assertEqual(response.status_code, 200)
        row = response.json()["results"][0]
        self.assertEqual(row["sale_price"], "150000.00")
        self.assertEqual([o["name"] for o in row["previous_owners"]], ["Old Owner LLC"])
        self.assertEqual([o["name"] for o in row["new_owners"]], ["New Owner LLC"])

    def test_excludes_sales_of_other_locations(self) -> None:
        baker.make(WikiPropertySale, location=baker.make("dashboard.Location"))
        response = self.client.get(self.url("sales/"), **self.headers())
        self.assertEqual(response.json()["count"], 0)


class WikiArticleRevisionDeleteTests(_WikiExtrasTestCase):
    """DELETE .../article/revisions/{id}/."""

    def _make_revision(self, *, editor: Profile | None, content: str = "text") -> ArticleRevision:
        _article, revision = save_article(editor=editor, content=content, wiki=self.wiki)
        assert revision is not None
        return revision

    def test_the_author_can_delete_their_own_revision(self) -> None:
        revision = self._make_revision(editor=self.profile)
        response = self.client.delete(self.url(f"article/revisions/{revision.pk}/"), **self.headers())

        self.assertEqual(response.status_code, 204)
        self.assertFalse(ArticleRevision.objects.filter(pk=revision.pk).exists())

    def test_deleting_a_revision_leaves_the_articles_current_text_untouched(self) -> None:
        """Article.content is its own denormalized field, not derived from history."""
        revision = self._make_revision(editor=self.profile, content="the current text")
        self.client.delete(self.url(f"article/revisions/{revision.pk}/"), **self.headers())

        article = get_article(wiki=self.wiki)
        assert article is not None
        self.assertEqual(article.content, "the current text")

    def test_cannot_delete_a_revision_authored_by_someone_else(self) -> None:
        other_user = baker.make(User)
        other_profile = Profile.objects.get(user=other_user)
        revision = self._make_revision(editor=other_profile)

        response = self.client.delete(self.url(f"article/revisions/{revision.pk}/"), **self.headers())

        self.assertEqual(response.status_code, 404)
        self.assertTrue(ArticleRevision.objects.filter(pk=revision.pk).exists())

    def test_cannot_delete_a_system_seeded_revision_with_no_editor(self) -> None:
        revision = self._make_revision(editor=None)
        response = self.client.delete(self.url(f"article/revisions/{revision.pk}/"), **self.headers())
        self.assertEqual(response.status_code, 404)

    def test_deleting_a_revision_nulls_out_restorations_that_pointed_to_it(self) -> None:
        """The FK's own on_delete=SET_NULL handles the dangling reference."""
        original = self._make_revision(editor=self.profile, content="v1")
        # A second revision so restoring "v1" actually differs from the
        # article's current content - restore_revision is a no-op (no new
        # revision) when it would restore the content already in place.
        self._make_revision(editor=self.profile, content="v2")
        article = get_article(wiki=self.wiki)
        assert article is not None
        _article, restore = restore_revision(scope_article=article, revision=original, editor=self.profile)
        assert restore is not None
        self.assertEqual(restore.restored_from_id, original.pk)

        self.client.delete(self.url(f"article/revisions/{original.pk}/"), **self.headers())

        restore.refresh_from_db()
        self.assertIsNone(restore.restored_from_id)

    def test_deleting_an_unknown_revision_is_404(self) -> None:
        response = self.client.delete(self.url("article/revisions/999999/"), **self.headers())
        self.assertEqual(response.status_code, 404)

    def test_requires_wiki_write(self) -> None:
        ApiKey.objects.filter(user=self.user).update(scopes=[ApiKeyScope.WIKI_READ.value])
        revision = self._make_revision(editor=self.profile)
        response = self.client.delete(self.url(f"article/revisions/{revision.pk}/"), **self.headers())
        self.assertEqual(response.status_code, 403)
