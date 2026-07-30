"""Tests for the external API's profile social-links endpoint.

Mirrors ``controllers.userprofile.ViewProfileView``'s own rule: a social link
carries no separate ``contact_visibility`` gate the way phone numbers and
Discord handles do - anyone who can see the profile at all sees its links.
Only the owner may ever write them, and PUT is a full replace (matching
``SafetyContactDefaultsSerializer``'s own precedent), so submitting a smaller
set than what's saved is how a platform gets removed.
"""

from __future__ import annotations

from django.contrib.auth.models import User
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.account.model import ApiKeyScope
from urbanlens.dashboard.models.profile.meta import VisibilityChoice
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.models.social_link.model import SocialLink
from urbanlens.dashboard.services.api_keys import generate_api_key


def _bearer(raw_key: str) -> dict:
    return {"HTTP_AUTHORIZATION": f"Bearer {raw_key}"}


def _key_with_scopes(user: User, *scopes: ApiKeyScope) -> str:
    api_key, raw_key = generate_api_key(user, "Test")
    api_key.scopes = [scope.value for scope in scopes]
    api_key.save(update_fields=["scopes"])
    return raw_key


class _SocialLinksTestCase(TestCase):
    """An owner and an unrelated bystander, both mutually visible and fully scoped."""

    def setUp(self) -> None:
        baker.make(User)  # first user is auto-promoted to bootstrap site admin
        self.user = baker.make(User, username="owner")
        self.profile = Profile.objects.get(user=self.user)
        self.other_user = baker.make(User, username="bystander")
        self.other = Profile.objects.get(user=self.other_user)
        for profile in (self.profile, self.other):
            profile.profile_visibility = VisibilityChoice.ANYONE
            profile.save(update_fields=["profile_visibility"])
        self.raw_key = _key_with_scopes(self.user, ApiKeyScope.SOCIAL_READ, ApiKeyScope.SOCIAL_WRITE, ApiKeyScope.PROFILE_READ)
        self.other_key = _key_with_scopes(self.other_user, ApiKeyScope.SOCIAL_READ, ApiKeyScope.SOCIAL_WRITE, ApiKeyScope.PROFILE_READ)
        self.url = self._links_url(self.profile)

    def _links_url(self, profile: Profile) -> str:
        return reverse("external_api:profiles.social_links", kwargs={"profile_slug": profile.slug or str(profile.uuid)})


class SocialLinksGetTests(_SocialLinksTestCase):
    """GET is open to anyone who can see the profile at all."""

    def test_empty_by_default(self) -> None:
        response = self.client.get(self.url, **_bearer(self.raw_key))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["links"], [])

    def test_returns_saved_links(self) -> None:
        SocialLink.objects.create(profile=self.profile, platform="instagram", handle="explorer")
        response = self.client.get(self.url, **_bearer(self.raw_key))
        self.assertEqual(response.status_code, 200)
        links = response.json()["links"]
        self.assertEqual(len(links), 1)
        self.assertEqual(links[0]["platform"], "instagram")
        self.assertEqual(links[0]["handle"], "explorer")
        self.assertEqual(links[0]["url"], "https://instagram.com/explorer")
        self.assertEqual(links[0]["display_name"], "Instagram")

    def test_discord_link_has_a_null_url(self) -> None:
        """Discord has no public profile URL to render."""
        SocialLink.objects.create(profile=self.profile, platform="discord", handle="explorer#1234")
        links = self.client.get(self.url, **_bearer(self.raw_key)).json()["links"]
        self.assertIsNone(links[0]["url"])

    def test_another_visible_profiles_links_are_readable(self) -> None:
        SocialLink.objects.create(profile=self.other, platform="bluesky", handle="explorer.bsky.social")
        response = self.client.get(self._links_url(self.other), **_bearer(self.raw_key))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["links"][0]["platform"], "bluesky")

    def test_invisible_profile_is_404(self) -> None:
        self.other.profile_visibility = VisibilityChoice.FRIENDS
        self.other.save(update_fields=["profile_visibility"])
        response = self.client.get(self._links_url(self.other), **_bearer(self.raw_key))
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json(), {"error": "No such profile."})

    def test_unknown_slug_is_404(self) -> None:
        url = reverse("external_api:profiles.social_links", kwargs={"profile_slug": "no-such-person"})
        self.assertEqual(self.client.get(url, **_bearer(self.raw_key)).status_code, 404)

    def test_get_requires_social_read(self) -> None:
        raw = _key_with_scopes(self.user, ApiKeyScope.PROFILE_READ)
        self.assertEqual(self.client.get(self.url, **_bearer(raw)).status_code, 403)


class SocialLinksPutTests(_SocialLinksTestCase):
    """PUT fully replaces the caller's own link set."""

    def test_put_creates_links(self) -> None:
        response = self.client.put(
            self.url,
            {"links": [{"platform": "instagram", "handle": "@explorer"}, {"platform": "website", "handle": "example.com/me"}]},
            content_type="application/json",
            **_bearer(self.raw_key),
        )
        self.assertEqual(response.status_code, 200)
        payload = {link["platform"]: link["handle"] for link in response.json()["links"]}
        self.assertEqual(payload["instagram"], "explorer")
        self.assertEqual(payload["website"], "https://example.com/me")

    def test_put_strips_a_leading_at_sign(self) -> None:
        self.client.put(self.url, {"links": [{"platform": "reddit", "handle": "@explorer"}]}, content_type="application/json", **_bearer(self.raw_key))
        link = SocialLink.objects.get(profile=self.profile, platform="reddit")
        self.assertEqual(link.handle, "explorer")

    def test_put_replaces_the_whole_set(self) -> None:
        """A platform omitted from the submission is removed, not left alone."""
        SocialLink.objects.create(profile=self.profile, platform="instagram", handle="old")
        response = self.client.put(self.url, {"links": [{"platform": "reddit", "handle": "explorer"}]}, content_type="application/json", **_bearer(self.raw_key))
        self.assertEqual(response.status_code, 200)
        platforms = {link["platform"] for link in response.json()["links"]}
        self.assertEqual(platforms, {"reddit"})
        self.assertFalse(SocialLink.objects.filter(profile=self.profile, platform="instagram").exists())

    def test_empty_links_clears_every_platform(self) -> None:
        SocialLink.objects.create(profile=self.profile, platform="instagram", handle="explorer")
        response = self.client.put(self.url, {"links": []}, content_type="application/json", **_bearer(self.raw_key))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["links"], [])
        self.assertFalse(SocialLink.objects.filter(profile=self.profile).exists())

    def test_put_rejects_an_invalid_handle(self) -> None:
        response = self.client.put(self.url, {"links": [{"platform": "reddit", "handle": "no"}]}, content_type="application/json", **_bearer(self.raw_key))
        self.assertEqual(response.status_code, 400)
        self.assertFalse(SocialLink.objects.filter(profile=self.profile).exists())

    def test_put_rejects_a_non_http_website(self) -> None:
        response = self.client.put(
            self.url, {"links": [{"platform": "website", "handle": "javascript:alert(1)"}]}, content_type="application/json", **_bearer(self.raw_key)
        )
        self.assertEqual(response.status_code, 400)

    def test_put_rejects_a_javascript_scheme_even_once_scheme_defaulting_is_applied(self) -> None:
        """A scheme-less submission defaults to https - that must not let a dangerous raw scheme sneak through first.

        Naively prepending "https://" to any scheme-less handle would turn
        this into "https://javascript:alert(1)", whose netloc parses to the
        deceptively harmless-looking hostname "javascript" - the raw scheme
        has to be checked before that prefixing happens.
        """
        response = self.client.put(
            self.url, {"links": [{"platform": "website", "handle": "data:text/html,<script>alert(1)</script>"}]}, content_type="application/json", **_bearer(self.raw_key)
        )
        self.assertEqual(response.status_code, 400)
        self.assertFalse(SocialLink.objects.filter(profile=self.profile).exists())

    def test_put_rejects_an_invalid_discord_handle(self) -> None:
        response = self.client.put(
            self.url, {"links": [{"platform": "discord", "handle": "no spaces!"}]}, content_type="application/json", **_bearer(self.raw_key)
        )
        self.assertEqual(response.status_code, 400)

    def test_put_accepts_a_valid_discord_handle(self) -> None:
        response = self.client.put(
            self.url, {"links": [{"platform": "discord", "handle": "explorer#1234"}]}, content_type="application/json", **_bearer(self.raw_key)
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["links"][0]["url"], None)

    def test_put_rejects_a_duplicated_platform(self) -> None:
        response = self.client.put(
            self.url,
            {"links": [{"platform": "instagram", "handle": "a"}, {"platform": "instagram", "handle": "b"}]},
            content_type="application/json",
            **_bearer(self.raw_key),
        )
        self.assertEqual(response.status_code, 400)

    def test_put_rejects_an_unknown_platform(self) -> None:
        response = self.client.put(self.url, {"links": [{"platform": "myspace", "handle": "explorer"}]}, content_type="application/json", **_bearer(self.raw_key))
        self.assertEqual(response.status_code, 400)

    def test_cannot_put_someone_elses_links(self) -> None:
        response = self.client.put(
            self._links_url(self.other), {"links": [{"platform": "instagram", "handle": "explorer"}]}, content_type="application/json", **_bearer(self.raw_key)
        )
        self.assertEqual(response.status_code, 404)
        self.assertFalse(SocialLink.objects.filter(profile=self.other).exists())

    def test_put_requires_social_write(self) -> None:
        raw = _key_with_scopes(self.user, ApiKeyScope.SOCIAL_READ, ApiKeyScope.PROFILE_READ)
        response = self.client.put(self.url, {"links": []}, content_type="application/json", **_bearer(raw))
        self.assertEqual(response.status_code, 403)
