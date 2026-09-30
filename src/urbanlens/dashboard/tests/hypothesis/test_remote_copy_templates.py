"""Panels that show a provider's picture name this site's copy of it, never the provider (P165)."""

from __future__ import annotations

import re

from django.contrib.auth.models import User
from django.template.loader import render_to_string
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.remote_image_copy.model import RemoteImageCopy
from urbanlens.dashboard.services.media.remote_copies import url_digest
from urbanlens.dashboard.templatetags.remote_copies import remote_copy

_REMOTE = "https://provider.test/picture.jpg"
_SRC = re.compile(r'\bsrc="([^"]*)"')


class RemoteCopyFilterTests(TestCase):
    def test_a_remote_image_becomes_a_copy(self) -> None:
        self.assertEqual(remote_copy(_REMOTE, "yelp"), reverse("media.remote_copy", args=[url_digest(_REMOTE)]))
        self.assertEqual(RemoteImageCopy.objects.get().provider, "yelp")

    def test_an_in_app_or_missing_image_is_left_alone(self) -> None:
        self.assertEqual(remote_copy("/media/pin_images/a.jpg", "search"), "/media/pin_images/a.jpg")
        self.assertEqual(remote_copy(None, "search"), "")
        self.assertFalse(RemoteImageCopy.objects.exists())


class PanelTemplateTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.pin = baker.make_recipe("dashboard.pin")

    def _sources(self, template: str, context: dict) -> list[str]:
        return _SRC.findall(render_to_string(f"dashboard/{template}", context))

    def test_no_panel_names_the_provider(self) -> None:
        asset = {"id": "1", "thumbnail_url": _REMOTE, "already_imported": False}
        cases = {
            "partials/pins/pin_yelp.html": {"business": {"name": "Diner", "image_url": _REMOTE}},
            "partials/pins/pin_usgs_topo.html": {"maps": [{"title": "Quad", "previewGraphicURL": _REMOTE}]},
            "partials/pins/pin_nps.html": {"park": {"full_name": "Park", "images": [{"url": _REMOTE}]}},
            "partials/pins/pin_nominatim.html": {"place": {"name": "Mill", "image": _REMOTE}},
            "partials/search/_panel.html": {
                "query": "mill",
                "response": {"groups": [{"label": "Places", "results": [{"title": "Mill", "image_url": _REMOTE}]}]},
            },
            "partials/pins/_flickr_picker_dialog.html": {
                "account": True,
                "pin": self.pin,
                "mode": "search",
                "assets": [asset],
            },
            "partials/pins/_flickr_album_dialog.html": {
                "flickr_configured": True,
                "pin": self.pin,
                "album": {"title": "A", "total": 1},
                "assets": [asset],
            },
        }
        copy = reverse("media.remote_copy", args=[url_digest(_REMOTE)])
        for template, context in cases.items():
            with self.subTest(template=template):
                sources = self._sources(template, {"pin": self.pin, **context})
                self.assertIn(copy, sources)
                self.assertFalse([source for source in sources if "provider.test" in source])


class GravatarPreviewTests(TestCase):
    def test_the_profile_editor_previews_gravatar_from_a_copy(self) -> None:
        baker.make(User)  # absorbs the bootstrap site-admin promotion
        user = baker.make(User, email="someone@example.test")
        self.client.force_login(user)

        response = self.client.get(reverse("profile.edit"))

        self.assertNotContains(response, "gravatar.com")
        copy = RemoteImageCopy.objects.get(provider="gravatar")
        self.assertTrue(copy.source_url.startswith("https://www.gravatar.com/avatar/"))
        self.assertTrue(copy.edition)
        self.assertContains(response, f'src="{reverse("media.remote_copy", args=[copy.url_digest])}"')
