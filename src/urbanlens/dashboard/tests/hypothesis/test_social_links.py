"""Tests for social_links service — URL parsing and profile link rendering.

Covers all nine platforms, the _clean_handle validator, handle security contract
(no HTML/path-traversal payloads survive), website length cap, fragment stripping,
and the get_profile_links() rendering helper.
"""
from __future__ import annotations

from unittest.mock import MagicMock

from hypothesis import given, settings as hyp_settings
from hypothesis import strategies as st

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.services.social_links import (
    PLATFORM_FA_ICON,
    PLATFORM_URL_TEMPLATE,
    _clean_handle,
    get_profile_links,
    parse_social_link,
)

_hyp = hyp_settings(max_examples=60, deadline=None)


# ---------------------------------------------------------------------------
# _clean_handle
# ---------------------------------------------------------------------------

class CleanHandleTests(TestCase):
    """_clean_handle strips leading @ and rejects invalid characters."""

    def test_plain_handle_unchanged(self):
        self.assertEqual(_clean_handle("johndoe"), "johndoe")

    def test_at_prefix_stripped(self):
        self.assertEqual(_clean_handle("@johndoe"), "johndoe")

    def test_none_returns_none(self):
        self.assertIsNone(_clean_handle(None))

    def test_empty_string_returns_none(self):
        self.assertIsNone(_clean_handle(""))

    def test_whitespace_only_returns_none(self):
        self.assertIsNone(_clean_handle("   "))

    def test_handle_with_dot_and_hyphen_valid(self):
        self.assertEqual(_clean_handle("john.doe-1"), "john.doe-1")

    def test_handle_with_hash_valid(self):
        self.assertEqual(_clean_handle("user#1234"), "user#1234")

    def test_handle_with_space_rejected(self):
        self.assertIsNone(_clean_handle("john doe"))

    def test_html_injection_rejected(self):
        self.assertIsNone(_clean_handle("<script>alert(1)</script>"))

    def test_path_traversal_rejected(self):
        self.assertIsNone(_clean_handle("../../etc/passwd"))

    def test_semicolon_rejected(self):
        self.assertIsNone(_clean_handle("user;DROP TABLE"))

    @given(
        handle=st.text(
            alphabet=st.characters(whitelist_categories=("Lu", "Ll", "Nd")),
            min_size=1,
            max_size=30,
        )
    )
    @_hyp
    def test_alphanumeric_handle_always_valid(self, handle: str):
        result = _clean_handle(handle)
        self.assertIsNotNone(result)

    @given(
        bad_char=st.characters(whitelist_categories=("Ps", "Pe", "Po")).filter(
            lambda c: c not in "._-@#"
        ),
        base=st.from_regex(r"[a-z]{3,10}", fullmatch=True),
    )
    @_hyp
    def test_handles_with_forbidden_chars_rejected(self, bad_char: str, base: str):
        handle = base + bad_char
        self.assertIsNone(_clean_handle(handle))


# ---------------------------------------------------------------------------
# Instagram
# ---------------------------------------------------------------------------

class InstagramParserTests(TestCase):
    """parse_social_link correctly extracts Instagram handles."""

    def test_standard_url(self):
        self.assertEqual(parse_social_link("https://instagram.com/johndoe"), ("instagram", "johndoe"))

    def test_www_prefix_ignored(self):
        self.assertEqual(parse_social_link("https://www.instagram.com/janedoe"), ("instagram", "janedoe"))

    def test_url_without_scheme(self):
        self.assertEqual(parse_social_link("instagram.com/myuser"), ("instagram", "myuser"))

    def test_trailing_slash_ignored(self):
        self.assertEqual(parse_social_link("https://instagram.com/johndoe/"), ("instagram", "johndoe"))

    def test_no_path_returns_none(self):
        self.assertIsNone(parse_social_link("https://instagram.com/"))

    def test_at_in_url_stripped(self):
        result = parse_social_link("https://instagram.com/@myhandle")
        self.assertEqual(result, ("instagram", "myhandle"))

    def test_handle_with_illegal_chars_returns_none(self):
        self.assertIsNone(parse_social_link("https://instagram.com/<script>"))


# ---------------------------------------------------------------------------
# Bluesky
# ---------------------------------------------------------------------------

class BlueskyParserTests(TestCase):
    """parse_social_link correctly extracts Bluesky profile handles."""

    def test_standard_profile_url(self):
        self.assertEqual(
            parse_social_link("https://bsky.app/profile/alice.bsky.social"),
            ("bluesky", "alice.bsky.social"),
        )

    def test_missing_profile_prefix_returns_none(self):
        self.assertIsNone(parse_social_link("https://bsky.app/alice.bsky.social"))

    def test_root_url_returns_none(self):
        self.assertIsNone(parse_social_link("https://bsky.app/"))


# ---------------------------------------------------------------------------
# UER
# ---------------------------------------------------------------------------

class UERParserTests(TestCase):
    """parse_social_link correctly extracts UER posterid."""

    def test_standard_query_url(self):
        self.assertEqual(
            parse_social_link("https://www.uer.ca/forum_showprofile.asp?fid=1&posterid=12345"),
            ("uer", "12345"),
        )

    def test_non_numeric_posterid_returns_none(self):
        self.assertIsNone(
            parse_social_link("https://www.uer.ca/forum_showprofile.asp?fid=1&posterid=abc")
        )

    def test_missing_posterid_returns_none(self):
        self.assertIsNone(parse_social_link("https://www.uer.ca/forum_showprofile.asp?fid=1"))


# ---------------------------------------------------------------------------
# Facebook
# ---------------------------------------------------------------------------

class FacebookParserTests(TestCase):
    """parse_social_link correctly extracts Facebook handles."""

    def test_facebook_com(self):
        self.assertEqual(parse_social_link("https://facebook.com/johndoe"), ("facebook", "johndoe"))

    def test_fb_com_alias(self):
        self.assertEqual(parse_social_link("https://fb.com/janedoe"), ("facebook", "janedoe"))

    def test_fb_me_alias(self):
        self.assertEqual(parse_social_link("https://fb.me/myhandle"), ("facebook", "myhandle"))

    def test_no_path_returns_none(self):
        self.assertIsNone(parse_social_link("https://facebook.com/"))


# ---------------------------------------------------------------------------
# Flickr
# ---------------------------------------------------------------------------

class FlickrParserTests(TestCase):
    """parse_social_link correctly extracts Flickr usernames."""

    def test_photos_path_valid(self):
        self.assertEqual(
            parse_social_link("https://flickr.com/photos/johndoe"),
            ("flickr", "johndoe"),
        )

    def test_missing_photos_prefix_returns_none(self):
        self.assertIsNone(parse_social_link("https://flickr.com/johndoe"))

    def test_root_url_returns_none(self):
        self.assertIsNone(parse_social_link("https://flickr.com/"))


# ---------------------------------------------------------------------------
# YouTube
# ---------------------------------------------------------------------------

class YouTubeParserTests(TestCase):
    """parse_social_link correctly extracts YouTube handles and channel IDs."""

    def test_at_handle(self):
        self.assertEqual(
            parse_social_link("https://youtube.com/@mychannel"),
            ("youtube", "mychannel"),
        )

    def test_channel_prefix(self):
        self.assertEqual(
            parse_social_link("https://youtube.com/channel/UC123abc"),
            ("youtube", "UC123abc"),
        )

    def test_user_prefix(self):
        self.assertEqual(
            parse_social_link("https://youtube.com/user/myoldchannel"),
            ("youtube", "myoldchannel"),
        )

    def test_c_prefix(self):
        self.assertEqual(
            parse_social_link("https://youtube.com/c/mychannel"),
            ("youtube", "mychannel"),
        )

    def test_root_url_returns_none(self):
        self.assertIsNone(parse_social_link("https://youtube.com/"))

    def test_youtu_be_without_at_returns_none(self):
        self.assertIsNone(parse_social_link("https://youtu.be/"))


# ---------------------------------------------------------------------------
# TikTok
# ---------------------------------------------------------------------------

class TikTokParserTests(TestCase):
    """parse_social_link correctly extracts TikTok handles."""

    def test_at_handle(self):
        self.assertEqual(
            parse_social_link("https://tiktok.com/@myuser"),
            ("tiktok", "myuser"),
        )

    def test_no_at_prefix_returns_none(self):
        self.assertIsNone(parse_social_link("https://tiktok.com/myuser"))

    def test_root_url_returns_none(self):
        self.assertIsNone(parse_social_link("https://tiktok.com/"))


# ---------------------------------------------------------------------------
# Reddit
# ---------------------------------------------------------------------------

class RedditParserTests(TestCase):
    """parse_social_link correctly extracts Reddit usernames."""

    def test_u_prefix(self):
        self.assertEqual(
            parse_social_link("https://reddit.com/u/johndoe"),
            ("reddit", "johndoe"),
        )

    def test_user_prefix(self):
        self.assertEqual(
            parse_social_link("https://reddit.com/user/johndoe"),
            ("reddit", "johndoe"),
        )

    def test_redd_it_alias(self):
        self.assertEqual(
            parse_social_link("https://redd.it/u/johndoe"),
            ("reddit", "johndoe"),
        )

    def test_missing_prefix_returns_none(self):
        self.assertIsNone(parse_social_link("https://reddit.com/johndoe"))

    def test_r_subreddit_returns_none(self):
        self.assertIsNone(parse_social_link("https://reddit.com/r/urbex"))


# ---------------------------------------------------------------------------
# Generic website
# ---------------------------------------------------------------------------

class WebsiteParserTests(TestCase):
    """parse_social_link falls through to generic website for unknown domains."""

    def test_arbitrary_https_site(self):
        result = parse_social_link("https://mysite.example.com/path")
        self.assertIsNotNone(result)
        platform, handle = result  # type: ignore
        self.assertEqual(platform, "website")
        self.assertIn("mysite.example.com", handle)

    def test_http_site_accepted(self):
        result = parse_social_link("http://example.com/")
        self.assertIsNotNone(result)
        self.assertEqual(result[0], "website")

    def test_fragment_stripped(self):
        result = parse_social_link("https://example.com/page#section")
        self.assertIsNotNone(result)
        _, handle = result
        self.assertNotIn("#section", handle)

    def test_url_over_500_chars_rejected(self):
        long_path = "a" * 490
        url = f"https://example.com/{long_path}"
        self.assertIsNone(parse_social_link(url))

    def test_url_exactly_500_chars_accepted(self):
        # Build a URL whose full string is exactly 500 chars.
        base = "https://example.com/"
        path = "a" * (500 - len(base))
        url = base + path
        self.assertEqual(len(url), 500)
        result = parse_social_link(url)
        self.assertIsNotNone(result)


# ---------------------------------------------------------------------------
# Rejected schemes / edge cases
# ---------------------------------------------------------------------------

class SecurityRejectionTests(TestCase):
    """parse_social_link rejects dangerous schemes and empty inputs."""

    def test_empty_string_returns_none(self):
        self.assertIsNone(parse_social_link(""))

    def test_whitespace_only_returns_none(self):
        self.assertIsNone(parse_social_link("   "))

    def test_javascript_scheme_rejected(self):
        self.assertIsNone(parse_social_link("javascript:alert(1)"))

    def test_data_uri_rejected(self):
        self.assertIsNone(parse_social_link("data:text/html,<h1>hi</h1>"))

    def test_ftp_scheme_rejected(self):
        self.assertIsNone(parse_social_link("ftp://example.com/file"))

    def test_mailto_rejected(self):
        self.assertIsNone(parse_social_link("mailto:user@example.com"))

    @given(
        scheme=st.sampled_from(["javascript", "vbscript", "data", "ftp", "file"]),
        payload=st.from_regex(r"[a-z]{3,10}", fullmatch=True),
    )
    @_hyp
    def test_non_http_schemes_always_rejected(self, scheme: str, payload: str):
        url = f"{scheme}:{payload}"
        self.assertIsNone(parse_social_link(url))


# ---------------------------------------------------------------------------
# get_profile_links
# ---------------------------------------------------------------------------

class GetProfileLinksTests(TestCase):
    """get_profile_links renders SocialLink rows into dicts with URL and icon."""

    def _make_link(self, platform: str, handle: str):
        link = MagicMock()
        link.platform = platform
        link.handle = handle
        return link

    def _make_profile(self, links: list) -> MagicMock:
        profile = MagicMock()
        profile.social_links.all.return_value.order_by.return_value = links
        return profile

    def test_instagram_url_built_correctly(self):
        profile = self._make_profile([self._make_link("instagram", "johndoe")])
        result = get_profile_links(profile)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["url"], "https://instagram.com/johndoe")
        self.assertEqual(result[0]["platform"], "instagram")
        self.assertEqual(result[0]["handle"], "johndoe")

    def test_discord_has_no_url(self):
        profile = self._make_profile([self._make_link("discord", "User#1234")])
        result = get_profile_links(profile)
        self.assertIsNone(result[0]["url"])

    def test_display_name_present(self):
        profile = self._make_profile([self._make_link("youtube", "mychannel")])
        result = get_profile_links(profile)
        self.assertEqual(result[0]["display_name"], "YouTube")

    def test_icon_key_present(self):
        for platform in PLATFORM_URL_TEMPLATE:
            with self.subTest(platform=platform):
                profile = self._make_profile([self._make_link(platform, "anyhandle")])
                result = get_profile_links(profile)
                self.assertIn("icon", result[0])
                self.assertEqual(result[0]["icon"], PLATFORM_FA_ICON.get(platform, "fa-solid fa-link"))

    def test_empty_links_returns_empty_list(self):
        profile = self._make_profile([])
        self.assertEqual(get_profile_links(profile), [])

    def test_website_url_is_handle_itself(self):
        profile = self._make_profile([self._make_link("website", "https://mysite.com")])
        result = get_profile_links(profile)
        self.assertEqual(result[0]["url"], "https://mysite.com")
