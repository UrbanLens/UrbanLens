"""Tests for the community-wiki notification on safety check-in escalation.

Covers:
- find_community_wiki: point-to-wiki resolution (50 m proximity, existing wikis only).
- post_checkin_to_community_wiki via escalate_checkin: the wiki comment, the
  pin-owner notifications/emails, per-user delivery preferences, and idempotency.
- SafetyCheckinWikiOptionView: the HTMX toggle fragment.
- SafetyCheckinCreateView: persisting the notify_community_wiki flag.
- SafetyCheckinDetailView: the read-only community status page for non-owners.
- render_comment_text: bare URLs (like the posted check-in link) become safe anchors.
"""

from __future__ import annotations

import datetime

from django.core import mail
from django.urls import reverse
from django.utils import timezone
from hypothesis import given, settings, strategies as st
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.comments.model import Comment
from urbanlens.dashboard.models.notifications.meta import DeliveryPreference, NotificationType
from urbanlens.dashboard.models.notifications.model import NotificationLog, NotificationPreference
from urbanlens.dashboard.models.safety.model import SafetyCheckin, SafetyCheckinStatus
from urbanlens.dashboard.services.mentions import render_comment_text
from urbanlens.dashboard.services.safety import escalate_checkin, find_community_wiki, post_checkin_to_community_wiki

WIKI_LAT = 40.0
WIKI_LNG = -74.0


def _checkin(profile, **kwargs) -> SafetyCheckin:
    defaults = {
        "profile": profile,
        "title": "Test hike",
        "checkin_by": timezone.now() - datetime.timedelta(hours=2),
        "grace_period": datetime.timedelta(hours=1),
        "destination_latitude": str(WIKI_LAT),
        "destination_longitude": str(WIKI_LNG),
        "status": SafetyCheckinStatus.AWAITING_CHECKIN,
    }
    defaults.update(kwargs)
    return baker.make("dashboard.SafetyCheckin", **defaults)


def _location_with_wiki(latitude: float = WIKI_LAT, longitude: float = WIKI_LNG):
    location = baker.make("dashboard.Location", latitude=latitude, longitude=longitude)
    wiki = baker.make("dashboard.Wiki", location=location, name="Old Mill")
    return location, wiki


def _set_pref(profile, value: str) -> None:
    NotificationPreference.objects.update_or_create(profile=profile, defaults={"wiki_safety_checkin": value})


class FindCommunityWikiTests(TestCase):
    """Point-to-wiki resolution rules."""

    def test_returns_wiki_covering_the_point(self):
        _location, wiki = _location_with_wiki()
        self.assertEqual(find_community_wiki(WIKI_LAT, WIKI_LNG), wiki)

    def test_none_when_location_has_no_wiki(self):
        baker.make("dashboard.Location", latitude=WIKI_LAT, longitude=WIKI_LNG)
        self.assertIsNone(find_community_wiki(WIKI_LAT, WIKI_LNG))

    def test_none_when_wiki_is_far_away(self):
        _location_with_wiki()
        self.assertIsNone(find_community_wiki(WIKI_LAT + 1.0, WIKI_LNG))

    def test_none_without_a_destination(self):
        _location_with_wiki()
        self.assertIsNone(find_community_wiki(None, None))


class EscalationWikiNotifyTests(TestCase):
    """The wiki comment and pin-owner notifications raised at escalation."""

    def setUp(self):
        self.owner = baker.make("auth.User", email="owner@example.com").profile
        self.location, self.wiki = _location_with_wiki()
        self.pin_owner = baker.make("auth.User", email="pinowner@example.com").profile
        baker.make("dashboard.Pin", profile=self.pin_owner, location=self.location)

    def _wiki_emails(self) -> list:
        return [m for m in mail.outbox if "Safety check-in posted to" in m.subject]

    def test_escalation_posts_comment_and_notifies_pin_owner(self):
        checkin = _checkin(self.owner, notify_community_wiki=True)

        escalate_checkin(checkin)

        checkin.refresh_from_db()
        self.assertIsNotNone(checkin.wiki_notified_at)

        comment = Comment.objects.get(wiki=self.wiki)
        self.assertEqual(comment.profile, self.owner)
        self.assertIn(str(checkin.uuid), comment.text)

        log = NotificationLog.objects.get(profile=self.pin_owner, notification_type=NotificationType.WIKI_SAFETY_CHECKIN)
        self.assertEqual(log.source_profile, self.owner)
        self.assertIn("Old Mill", log.title)

        wiki_emails = self._wiki_emails()
        self.assertEqual(len(wiki_emails), 1)
        self.assertEqual(wiki_emails[0].to, ["pinowner@example.com"])

    def test_owner_is_not_notified_about_their_own_checkin(self):
        baker.make("dashboard.Pin", profile=self.owner, location=self.location)
        checkin = _checkin(self.owner, notify_community_wiki=True)

        escalate_checkin(checkin)

        self.assertFalse(NotificationLog.objects.filter(profile=self.owner, notification_type=NotificationType.WIKI_SAFETY_CHECKIN).exists())
        self.assertNotIn(["owner@example.com"], [m.to for m in self._wiki_emails()])

    def test_pref_none_suppresses_notification_and_email(self):
        _set_pref(self.pin_owner, DeliveryPreference.NONE)
        checkin = _checkin(self.owner, notify_community_wiki=True)

        escalate_checkin(checkin)

        # The comment is still posted - the preference only governs personal alerts.
        self.assertTrue(Comment.objects.filter(wiki=self.wiki).exists())
        self.assertFalse(NotificationLog.objects.filter(profile=self.pin_owner, notification_type=NotificationType.WIKI_SAFETY_CHECKIN).exists())
        self.assertEqual(self._wiki_emails(), [])

    def test_pref_site_only_skips_the_email(self):
        _set_pref(self.pin_owner, DeliveryPreference.SITE)
        checkin = _checkin(self.owner, notify_community_wiki=True)

        escalate_checkin(checkin)

        self.assertTrue(NotificationLog.objects.filter(profile=self.pin_owner, notification_type=NotificationType.WIKI_SAFETY_CHECKIN).exists())
        self.assertEqual(self._wiki_emails(), [])

    def test_pref_email_only_skips_the_site_notification(self):
        _set_pref(self.pin_owner, DeliveryPreference.EMAIL)
        checkin = _checkin(self.owner, notify_community_wiki=True)

        escalate_checkin(checkin)

        self.assertFalse(NotificationLog.objects.filter(profile=self.pin_owner, notification_type=NotificationType.WIKI_SAFETY_CHECKIN).exists())
        self.assertEqual(len(self._wiki_emails()), 1)

    def test_no_wiki_post_when_owner_did_not_opt_in(self):
        checkin = _checkin(self.owner, notify_community_wiki=False)

        escalate_checkin(checkin)

        checkin.refresh_from_db()
        self.assertIsNone(checkin.wiki_notified_at)
        self.assertFalse(Comment.objects.filter(wiki=self.wiki).exists())

    def test_no_wiki_post_when_destination_has_no_wiki(self):
        checkin = _checkin(self.owner, notify_community_wiki=True, destination_latitude=str(WIKI_LAT + 5), destination_longitude=str(WIKI_LNG + 5))

        escalate_checkin(checkin)

        checkin.refresh_from_db()
        self.assertEqual(checkin.status, SafetyCheckinStatus.OVERDUE)
        self.assertIsNone(checkin.wiki_notified_at)
        self.assertFalse(Comment.objects.filter(wiki=self.wiki).exists())

    def test_wiki_post_is_idempotent(self):
        checkin = _checkin(self.owner, notify_community_wiki=True)

        post_checkin_to_community_wiki(checkin)
        post_checkin_to_community_wiki(checkin)

        self.assertEqual(Comment.objects.filter(wiki=self.wiki).count(), 1)
        self.assertEqual(NotificationLog.objects.filter(profile=self.pin_owner, notification_type=NotificationType.WIKI_SAFETY_CHECKIN).count(), 1)


class WikiOptionEndpointTests(TestCase):
    """The HTMX toggle fragment at /safety/wiki-option/."""

    def setUp(self):
        self.user = baker.make("auth.User")
        self.client.force_login(self.user)
        _location_with_wiki()

    def test_shows_toggle_when_destination_has_a_wiki(self):
        response = self.client.get(reverse("safety.checkin.wiki_option"), {"destination_latitude": WIKI_LAT, "destination_longitude": WIKI_LNG})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "notify_community_wiki")
        self.assertContains(response, "Old Mill")
        self.assertNotContains(response, "checked")

    def test_preserves_checked_state_across_refetches(self):
        response = self.client.get(
            reverse("safety.checkin.wiki_option"),
            {"destination_latitude": WIKI_LAT, "destination_longitude": WIKI_LNG, "notify_community_wiki": "1"},
        )
        self.assertContains(response, "checked")

    def test_empty_when_no_wiki_covers_the_destination(self):
        response = self.client.get(reverse("safety.checkin.wiki_option"), {"destination_latitude": WIKI_LAT + 1, "destination_longitude": WIKI_LNG})
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "notify_community_wiki")

    def test_empty_on_missing_or_invalid_coordinates(self):
        response = self.client.get(reverse("safety.checkin.wiki_option"), {"destination_latitude": "not-a-number"})
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "notify_community_wiki")


class CreateCheckinFlagTests(TestCase):
    """The create form persists the notify_community_wiki checkbox."""

    def setUp(self):
        self.user = baker.make("auth.User")
        self.client.force_login(self.user)

    def _post(self, extra: dict) -> None:
        checkin_by = (timezone.now() + datetime.timedelta(hours=3)).isoformat()
        data = {"checkin_by": checkin_by, "title": "Trip", "destination_latitude": str(WIKI_LAT), "destination_longitude": str(WIKI_LNG), **extra}
        response = self.client.post(reverse("safety.checkin.create"), data)
        self.assertEqual(response.status_code, 302)

    def test_checkbox_on_sets_the_flag(self):
        self._post({"notify_community_wiki": "1"})
        self.assertTrue(SafetyCheckin.objects.get(profile=self.user.profile).notify_community_wiki)

    def test_checkbox_absent_leaves_the_flag_off(self):
        self._post({})
        self.assertFalse(SafetyCheckin.objects.get(profile=self.user.profile).notify_community_wiki)


class CommunityStatusPageTests(TestCase):
    """Non-owner access to a wiki-posted check-in's limited status page."""

    def setUp(self):
        self.owner = baker.make("auth.User").profile
        self.visitor = baker.make("auth.User")
        self.client.force_login(self.visitor)
        _location_with_wiki()

    def test_non_owner_sees_community_page_for_wiki_posted_checkin(self):
        checkin = _checkin(self.owner, notify_community_wiki=True, wiki_notified_at=timezone.now(), plan_details="Secret plan", contact_message="Private message")
        response = self.client.get(reverse("safety.checkin.detail", kwargs={"checkin_slug": str(checkin.uuid)}))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, checkin.title)
        # Owner/contact-only details stay off the community page.
        self.assertNotContains(response, "Secret plan")
        self.assertNotContains(response, "Private message")

    def test_non_owner_gets_404_when_checkin_was_not_wiki_posted(self):
        checkin = _checkin(self.owner, notify_community_wiki=True, wiki_notified_at=None)
        response = self.client.get(reverse("safety.checkin.detail", kwargs={"checkin_slug": str(checkin.uuid)}))
        self.assertEqual(response.status_code, 404)

    def test_owner_still_gets_the_full_detail_page(self):
        checkin = _checkin(self.owner, notify_community_wiki=True, wiki_notified_at=timezone.now())
        self.client.force_login(self.owner.user)
        response = self.client.get(reverse("safety.checkin.detail", kwargs={"checkin_slug": str(checkin.uuid)}))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "safety-checkin-form")


class CommentUrlRenderingTests(TestCase):
    """Bare URLs in comment text (e.g. the posted check-in link) render as safe anchors."""

    def test_url_becomes_anchor(self):
        html = render_comment_text("See https://example.com/safety/abc/ for details", set())
        self.assertIn('<a href="https://example.com/safety/abc/"', html)

    def test_html_is_still_escaped(self):
        html = render_comment_text("<script>alert(1)</script> https://example.com/x", set())
        self.assertNotIn("<script>", html)
        self.assertIn("&lt;script&gt;", html)


@settings(max_examples=25, deadline=None)
@given(prefix=st.text(alphabet=st.characters(blacklist_categories=("Cs",), blacklist_characters="@"), max_size=40))
def test_render_comment_text_always_links_appended_urls(prefix: str) -> None:
    """Whatever surrounds it, a whitespace-delimited URL is rendered as an anchor, never markup-injected."""
    html = render_comment_text(f"{prefix} https://example.com/checkin/ ", set())
    assert html is not None  # nosec B101
    assert '<a href="https://example.com/checkin/"' in html  # nosec B101
    assert "<script" not in html.replace("&lt;script", "")  # nosec B101
