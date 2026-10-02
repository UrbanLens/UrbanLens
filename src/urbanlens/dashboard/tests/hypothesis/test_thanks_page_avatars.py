"""The Thanks page shows contributors' GitHub avatars from this site's copies, which img-src admits (P165)."""

from __future__ import annotations

from unittest.mock import patch

from django.urls import reverse

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.remote_image_copy.model import RemoteImageCopy
from urbanlens.dashboard.services.apis.infra.github.contributors import GitHubContributor
from urbanlens.dashboard.services.media.remote_copies import url_digest

_AVATAR = "https://avatars.githubusercontent.com/u/1?v=4"


class ThanksPageAvatarTests(TestCase):
    def test_a_contributors_avatar_is_this_sites_copy(self) -> None:
        contributor = GitHubContributor(
            login="octo", profile_url="https://github.com/octo", avatar_url=_AVATAR, contributions=3
        )
        with patch("urbanlens.dashboard.controllers.thanks.get_github_contributors", return_value=[contributor]):
            response = self.client.get(reverse("thanks"))

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "avatars.githubusercontent.com")
        self.assertContains(response, reverse("media.remote_copy", args=[url_digest(_AVATAR)]))
        self.assertEqual(RemoteImageCopy.objects.get().provider, "github")
