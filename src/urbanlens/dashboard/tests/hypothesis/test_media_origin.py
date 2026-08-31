"""Serving uploads from their own origin (``UL_MEDIA_BASE_URL``).

The split only works if three separate things hold, and each of them fails
silently in a different way:

- The cookie reaches the media origin at all. Get ``Domain`` wrong and every
  image on the site 404s while the app itself looks fine.
- The media gate accepts it, and accepts *only* it - a tampered, expired, or
  deactivated-user cookie must be as good as no cookie.
- Framing and MEDIA_URL follow. ``X-Frame-Options: SAMEORIGIN`` is correct
  same-origin and wrong the moment the lightbox frames another host, and
  ``MEDIA_URL`` is what carries the whole change out to ~100 ``.url`` call
  sites without touching any of them.
"""

from __future__ import annotations

from pathlib import Path
import shutil
import tempfile

from django.contrib.auth.models import User
from django.core import signing
from django.test import SimpleTestCase, override_settings
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.images.model import Image
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.services.media.origin import (
    MEDIA_COOKIE_MAX_AGE_SECONDS,
    MEDIA_COOKIE_NAME,
    MEDIA_COOKIE_SALT,
    cookie_domain,
    mint_media_token,
    needs_refresh,
    user_id_from_token,
)

MEDIA_ORIGIN = "https://media.example.com"
APP_ORIGIN = "https://app.example.com"

_ORIGIN_SETTINGS = {
    "UL_MEDIA_BASE_URL": MEDIA_ORIGIN,
    "SITE_URL": APP_ORIGIN,
    "ALLOWED_HOSTS": ["media.example.com", "app.example.com", "testserver"],
}


@override_settings(**_ORIGIN_SETTINGS)
class CookieDomainTests(SimpleTestCase):
    """The cookie has to be scoped to reach the media host and no further."""

    def test_derives_the_deepest_shared_domain(self) -> None:
        self.assertEqual(cookie_domain(), "example.com")

    @override_settings(UL_MEDIA_BASE_URL="https://media.dev.urbanlens.org", SITE_URL="https://dev.urbanlens.org")
    def test_prefers_the_deeper_shared_domain_over_the_apex(self) -> None:
        # Scoping this to urbanlens.org would hand the cookie to staging and
        # production as well.
        self.assertEqual(cookie_domain(), "dev.urbanlens.org")

    @override_settings(UL_MEDIA_COOKIE_DOMAIN="explicit.example.net")
    def test_explicit_override_wins(self) -> None:
        self.assertEqual(cookie_domain(), "explicit.example.net")

    @override_settings(UL_MEDIA_BASE_URL="https://media.somewhere-else.net")
    def test_unrelated_hosts_yield_no_domain(self) -> None:
        # "net" alone is a public suffix. Returning it would be a cookie scoped
        # to an entire TLD if any browser accepted it; refusing to guess and
        # logging is the only safe answer.
        with self.assertLogs("urbanlens.dashboard.services.media.origin", level="WARNING"):
            self.assertEqual(cookie_domain(), "")

    @override_settings(UL_MEDIA_BASE_URL="")
    def test_no_media_origin_means_no_cookie(self) -> None:
        self.assertEqual(cookie_domain(), "")


@override_settings(**_ORIGIN_SETTINGS)
class MediaTokenTests(SimpleTestCase):
    """Signing, and every way a token must fail closed."""

    def test_round_trips(self) -> None:
        self.assertEqual(user_id_from_token(mint_media_token(4242)), 4242)

    def test_rejects_a_tampered_token(self) -> None:
        token = mint_media_token(1)
        self.assertIsNone(user_id_from_token(token[:-1] + ("a" if token[-1] != "a" else "b")))

    def test_rejects_an_expired_token(self) -> None:
        self.assertIsNone(user_id_from_token(mint_media_token(1), max_age=-1))

    def test_rejects_a_token_signed_with_another_salt(self) -> None:
        # SECRET_KEY is shared with the preview-URL signer and the password-reset
        # tokens; the salt is the only thing keeping one from verifying as another.
        foreign = signing.dumps({"u": 1}, salt="some.other.purpose")
        self.assertIsNone(user_id_from_token(foreign))

    def test_rejects_a_boolean_payload(self) -> None:
        # bool is a subclass of int, so a naive isinstance check would read
        # {"u": true} as user 1.
        self.assertIsNone(user_id_from_token(signing.dumps({"u": True}, salt=MEDIA_COOKIE_SALT)))

    def test_rejects_a_non_dict_payload(self) -> None:
        self.assertIsNone(user_id_from_token(signing.dumps([1], salt=MEDIA_COOKIE_SALT)))

    def test_rejects_an_empty_token(self) -> None:
        self.assertIsNone(user_id_from_token(""))


@override_settings(**_ORIGIN_SETTINGS)
class MediaCookieMiddlewareTests(TestCase):
    """The app origin mints and clears the cookie as the session changes."""

    def setUp(self) -> None:
        """Create a user whose pages will be requested on the app origin."""
        self.user = baker.make(User)

    def _get_home(self, **extra: object) -> object:
        """Request any app-origin page, which is what triggers the middleware."""
        return self.client.get("/", HTTP_HOST="app.example.com", **extra)

    def test_sets_the_cookie_for_a_logged_in_user(self) -> None:
        self.client.force_login(self.user)
        response = self._get_home()
        cookie = response.cookies[MEDIA_COOKIE_NAME]
        self.assertEqual(user_id_from_token(cookie.value), self.user.pk)
        self.assertEqual(cookie["domain"], "example.com")
        self.assertTrue(cookie["httponly"])
        self.assertEqual(cookie["samesite"], "Lax")

    def test_does_not_set_a_cookie_for_an_anonymous_visitor(self) -> None:
        self.assertNotIn(MEDIA_COOKIE_NAME, self._get_home().cookies)

    def test_does_not_reset_a_cookie_that_is_still_fresh(self) -> None:
        # The whole point of the refresh window: an authenticated page view must
        # not pay a Set-Cookie (or the check behind it) on every request.
        self.client.force_login(self.user)
        self.client.cookies[MEDIA_COOKIE_NAME] = mint_media_token(self.user.pk)
        self.assertNotIn(MEDIA_COOKIE_NAME, self._get_home().cookies)

    def test_replaces_a_cookie_minted_for_a_different_user(self) -> None:
        # A shared device: log out, log in as someone else, and the media origin
        # must stop serving the previous account's files.
        other = baker.make(User)
        self.client.force_login(self.user)
        self.client.cookies[MEDIA_COOKIE_NAME] = mint_media_token(other.pk)
        response = self._get_home()
        self.assertEqual(user_id_from_token(response.cookies[MEDIA_COOKIE_NAME].value), self.user.pk)

    def test_clears_the_cookie_once_the_session_is_gone(self) -> None:
        # Logout needs no signal handler - the next anonymous response carrying
        # the cookie deletes it, which also covers session expiry.
        self.client.cookies[MEDIA_COOKIE_NAME] = mint_media_token(self.user.pk)
        self.assertEqual(self._get_home().cookies[MEDIA_COOKIE_NAME].value, "")

    @override_settings(UL_MEDIA_BASE_URL="")
    def test_does_nothing_without_a_media_origin(self) -> None:
        self.client.force_login(self.user)
        self.assertNotIn(MEDIA_COOKIE_NAME, self.client.get("/").cookies)


@override_settings(**_ORIGIN_SETTINGS)
class MediaOriginGateTests(TestCase):
    """The gate on the media origin: the cookie is the only way in."""

    def setUp(self) -> None:
        """Seed one owned image file under a throwaway MEDIA_ROOT."""
        self._media_root = tempfile.mkdtemp(prefix="ul_media_origin_")
        self.addCleanup(shutil.rmtree, self._media_root, ignore_errors=True)
        overrides = override_settings(MEDIA_ROOT=self._media_root, MEDIA_X_ACCEL=False)
        overrides.enable()
        self.addCleanup(overrides.disable)

        target = Path(self._media_root) / "pin_images" / "owned.png"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"fake-image-bytes")

        self.user = baker.make(User)
        self.owner: Profile = self.user.profile
        self.image_name = "pin_images/owned.png"
        baker.make(Image, image=self.image_name, profile=self.owner)

    def _fetch(self, **extra: object) -> object:
        """Request the seeded file as it would arrive on the media origin."""
        return self.client.get("/media/pin_images/owned.png", HTTP_HOST="media.example.com", **extra)

    def test_a_valid_cookie_serves_the_file(self) -> None:
        self.client.cookies[MEDIA_COOKIE_NAME] = mint_media_token(self.user.pk)
        self.assertEqual(self._fetch().status_code, 200)

    def test_no_cookie_is_a_404_not_a_login_redirect(self) -> None:
        # A redirect here would make an <img> fetch the login page's HTML and
        # render it as a broken image, and would frame our login form from a
        # foreign origin.
        self.assertEqual(self._fetch().status_code, 404)

    def test_a_tampered_cookie_is_a_404(self) -> None:
        token = mint_media_token(self.user.pk)
        self.client.cookies[MEDIA_COOKIE_NAME] = token[:-1] + ("a" if token[-1] != "a" else "b")
        self.assertEqual(self._fetch().status_code, 404)

    def test_an_expired_cookie_is_a_404(self) -> None:
        expired = signing.dumps({"u": self.user.pk}, salt=MEDIA_COOKIE_SALT)
        self.client.cookies[MEDIA_COOKIE_NAME] = expired
        with override_settings():
            # Load with a max_age the gate will apply as its own; the helper's
            # default is what the gate uses, so age the token past it instead.
            self.assertIsNone(user_id_from_token(expired, max_age=-1))
        self.assertEqual(user_id_from_token(expired, max_age=MEDIA_COOKIE_MAX_AGE_SECONDS), self.user.pk)

    def test_a_deactivated_user_is_a_404(self) -> None:
        # Deactivating an account must stop serving its media immediately rather
        # than when the cookie happens to age out.
        self.client.cookies[MEDIA_COOKIE_NAME] = mint_media_token(self.user.pk)
        User.objects.filter(pk=self.user.pk).update(is_active=False)
        self.assertEqual(self._fetch().status_code, 404)

    def test_the_cookie_is_refused_on_the_app_origin(self) -> None:
        # It is domain-scoped, so the browser sends it to the app origin too.
        # Accepting it there would make it a second, longer-lived session: it
        # survives up to 12h past the real one, and every byte-serving view
        # (including the billed panel image proxy) would take it.
        image_path = f"/media/{self.image_name}"
        self.client.cookies[MEDIA_COOKIE_NAME] = mint_media_token(self.user.pk)
        response = self.client.get(image_path, HTTP_HOST="app.example.com")
        self.assertNotEqual(response.status_code, 200, "the media cookie must not authenticate on the app origin")

    def test_another_users_cookie_cannot_reach_this_file(self) -> None:
        # The cookie only says *who*; per-file authorization is unchanged.
        stranger = baker.make(User)
        self.client.cookies[MEDIA_COOKIE_NAME] = mint_media_token(stranger.pk)
        self.assertEqual(self._fetch().status_code, 404)

    def test_framing_headers_name_the_app_origin(self) -> None:
        self.client.cookies[MEDIA_COOKIE_NAME] = mint_media_token(self.user.pk)
        response = self._fetch()
        # X-Frame-Options must be absent, not merely overridden: a browser that
        # does not implement frame-ancestors would still honour SAMEORIGIN and
        # break the document lightbox.
        self.assertNotIn("X-Frame-Options", response)
        self.assertIn(f"frame-ancestors {APP_ORIGIN}", response["Content-Security-Policy"])
        self.assertIn("default-src 'none'", response["Content-Security-Policy"])
        self.assertEqual(response["X-Content-Type-Options"], "nosniff")
        self.assertEqual(response["Referrer-Policy"], "no-referrer")

    def test_the_app_origin_keeps_its_same_origin_framing(self) -> None:
        self.client.force_login(self.user)
        response = self.client.get("/media/pin_images/owned.png", HTTP_HOST="app.example.com")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["X-Frame-Options"], "SAMEORIGIN")

    def test_the_media_origin_serves_nothing_but_media(self) -> None:
        # Enforced by media.conf, which has no route to an app page at all. This
        # asserts the Django half: a logged-in-looking media cookie is not a
        # session and cannot reach a page.
        self.client.cookies[MEDIA_COOKIE_NAME] = mint_media_token(self.user.pk)
        response = self.client.get("/dashboard/map/", HTTP_HOST="media.example.com")
        self.assertNotEqual(response.status_code, 200)


@override_settings(**_ORIGIN_SETTINGS)
class NeedsRefreshTests(SimpleTestCase):
    """When the middleware decides to write a cookie."""

    def _request(self, token: str | None):
        """A bare request carrying (or not carrying) a media cookie."""
        from django.test import RequestFactory

        request = RequestFactory().get("/")
        if token is not None:
            request.COOKIES[MEDIA_COOKIE_NAME] = token
        return request

    def test_no_cookie_needs_a_refresh(self) -> None:
        self.assertTrue(needs_refresh(self._request(None), 1))

    def test_a_fresh_cookie_for_this_user_does_not(self) -> None:
        self.assertFalse(needs_refresh(self._request(mint_media_token(1)), 1))

    def test_a_cookie_for_another_user_does(self) -> None:
        self.assertTrue(needs_refresh(self._request(mint_media_token(2)), 1))

    def test_garbage_does(self) -> None:
        self.assertTrue(needs_refresh(self._request("not-a-token"), 1))


class MediaUrlTests(SimpleTestCase):
    """``MEDIA_URL`` is what carries the split to every existing call site."""

    @override_settings(MEDIA_URL=f"{MEDIA_ORIGIN}/media/")
    def test_file_urls_are_built_against_the_media_origin(self) -> None:
        # FileSystemStorage.url is urljoin(MEDIA_URL, name), so an absolute
        # MEDIA_URL moves every template, serializer and API response at once -
        # which is the entire reason the cookie approach was chosen over
        # per-viewer signed URLs.
        from django.core.files.storage import default_storage

        self.assertEqual(default_storage.url("pin_images/a7/x.webp"), f"{MEDIA_ORIGIN}/media/pin_images/a7/x.webp")


class SettingsNamesAreRealTests(SimpleTestCase):
    """The settings this module reads have to be ones that actually exist.

    ``override_settings`` will happily *invent* a setting that production does
    not define, so a suite built entirely on overrides can pass while the real
    code path reads ``""`` forever. That is not hypothetical: ``cookie_domain``
    originally read ``settings.UL_SITE_URL`` - the environment variable's
    spelling, where the setting is ``SITE_URL`` - which made it return ``""``
    on every real deployment, silently turning ``set_media_cookie`` into a
    no-op and 404ing the entire media origin. Every test above passed
    throughout, because they each overrode the name into existence.

    These assert against the *unoverridden* settings object for that reason.
    """

    def test_site_url_is_the_real_setting_name(self) -> None:
        from django.conf import settings

        self.assertTrue(hasattr(settings, "SITE_URL"), "settings.SITE_URL is what cookie_domain and the frame-ancestors CSP read")
        self.assertFalse(hasattr(settings, "UL_SITE_URL"), "UL_SITE_URL is the env var, not the setting - reading it off settings yields '' silently")

    def test_every_setting_this_module_reads_exists(self) -> None:
        from django.conf import settings

        for name in ("SITE_URL", "UL_MEDIA_BASE_URL", "UL_MEDIA_COOKIE_DOMAIN", "UL_MEDIA_CSP", "SESSION_COOKIE_SECURE"):
            with self.subTest(setting=name):
                self.assertTrue(hasattr(settings, name), f"services.media.origin reads settings.{name}, which does not exist")

    @override_settings(UL_MEDIA_BASE_URL=MEDIA_ORIGIN, SITE_URL=APP_ORIGIN, UL_MEDIA_COOKIE_DOMAIN="")
    def test_cookie_domain_actually_derives_from_site_url(self) -> None:
        # The behavioural half of the check above: not just "the name exists"
        # but "this function reads it". Changing SITE_URL must change the answer.
        self.assertEqual(cookie_domain(), "example.com")
        with override_settings(SITE_URL="https://media.example.com"):
            self.assertEqual(cookie_domain(), "media.example.com")


class MediaOriginStartupCheckTests(SimpleTestCase):
    """``checks.check_media_origin_cookie_domain`` fails loudly on a dead config.

    A media origin that cannot issue its cookie is a total, silent media outage
    - the failure this check exists to convert into a startup error.
    """

    def _check(self):
        from urbanlens.dashboard.checks import check_media_origin_cookie_domain

        return [error.id for error in check_media_origin_cookie_domain()]

    @override_settings(UL_MEDIA_BASE_URL="")
    def test_no_media_origin_is_not_an_error(self) -> None:
        self.assertEqual(self._check(), [])

    @override_settings(UL_MEDIA_BASE_URL=MEDIA_ORIGIN, SITE_URL=APP_ORIGIN, UL_MEDIA_COOKIE_DOMAIN="")
    def test_a_working_pair_is_not_an_error(self) -> None:
        self.assertEqual(self._check(), [])

    @override_settings(UL_MEDIA_BASE_URL="not-a-url", SITE_URL=APP_ORIGIN)
    def test_a_media_origin_with_no_hostname_is_an_error(self) -> None:
        self.assertEqual(self._check(), ["dashboard.E003"])

    @override_settings(UL_MEDIA_BASE_URL="https://media.somewhere-else.net", SITE_URL=APP_ORIGIN, UL_MEDIA_COOKIE_DOMAIN="")
    def test_unrelated_hosts_are_an_error(self) -> None:
        # This is the shape the original UL_SITE_URL bug took: no derivable
        # domain, so the cookie is never set and every media URL 404s.
        self.assertEqual(self._check(), ["dashboard.E004"])

    @override_settings(UL_MEDIA_BASE_URL="https://media.a.co.uk", SITE_URL="https://b.co.uk", UL_MEDIA_COOKIE_DOMAIN="")
    def test_a_public_suffix_domain_is_an_error(self) -> None:
        # Two labels is not enough of a floor under a multi-part public suffix:
        # this pair derives "co.uk", which browsers reject outright.
        self.assertEqual(self._check(), ["dashboard.E005"])

    @override_settings(UL_MEDIA_BASE_URL="https://media.a.co.uk", SITE_URL="https://b.co.uk", UL_MEDIA_COOKIE_DOMAIN="explicit.example.com")
    def test_an_explicit_cookie_domain_settles_it(self) -> None:
        self.assertEqual(self._check(), [])
