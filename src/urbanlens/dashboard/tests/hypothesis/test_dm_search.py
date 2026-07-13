"""Tests for the Messages page's own search - "search this conversation" and
"search all conversations".

Both features are thin wrappers around ``services.direct_messages.search_direct_messages``,
which reuses global search's natural-language parser
(``services.global_search.parser.parse_query``) and shares its DirectMessage
queryset builder (``services.direct_messages.message_search_queryset``) with
``services.global_search.providers.DirectMessageSearchProvider`` - so this
covers the scoping/encryption/deletion rules once here rather than duplicating
global search's own ``DirectMessageSearchTests``.
"""

from __future__ import annotations

from datetime import UTC, datetime

from django.urls import reverse
from django.utils import timezone
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.services.direct_messages import search_direct_messages


class SearchDirectMessagesTests(TestCase):
    """search_direct_messages: scoping, encryption, deletion, and NL filters."""

    def setUp(self) -> None:
        super().setUp()
        self.alice = baker.make("auth.User", username="alice").profile
        self.bob = baker.make("auth.User", username="bob").profile
        self.eve = baker.make("auth.User", username="eve").profile

    def test_finds_matching_plaintext_message(self) -> None:
        baker.make("dashboard.DirectMessage", sender=self.alice, recipient=self.bob, body="Meet at the old asylum gate")
        hits = search_direct_messages(self.bob, "asylum gate")
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["partner"], self.alice)

    def test_encrypted_message_is_not_searchable(self) -> None:
        baker.make("dashboard.DirectMessage", sender=self.alice, recipient=self.bob, body="", ciphertext="deadbeef", nonce="abc", key_version=1)
        self.assertEqual(search_direct_messages(self.bob, "deadbeef"), [])

    def test_message_deleted_for_self_is_excluded_for_that_viewer_only(self) -> None:
        baker.make("dashboard.DirectMessage", sender=self.alice, recipient=self.bob, body="secret plans", deleted_by_recipient_at=timezone.now())
        self.assertEqual(search_direct_messages(self.bob, "secret plans"), [])
        self.assertEqual(len(search_direct_messages(self.alice, "secret plans")), 1)

    def test_conversation_scope_excludes_other_partners(self) -> None:
        baker.make("dashboard.DirectMessage", sender=self.alice, recipient=self.bob, body="shared photos of the mill")
        baker.make("dashboard.DirectMessage", sender=self.eve, recipient=self.bob, body="shared photos too")
        hits = search_direct_messages(self.bob, "photos", partner=self.alice)
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["partner"], self.alice)

    def test_unscoped_search_includes_every_partner(self) -> None:
        baker.make("dashboard.DirectMessage", sender=self.alice, recipient=self.bob, body="shared photos of the mill")
        baker.make("dashboard.DirectMessage", sender=self.eve, recipient=self.bob, body="shared photos too")
        self.assertEqual(len(search_direct_messages(self.bob, "photos")), 2)

    def test_blank_query_returns_no_hits(self) -> None:
        baker.make("dashboard.DirectMessage", sender=self.alice, recipient=self.bob, body="anything")
        self.assertEqual(search_direct_messages(self.bob, "   "), [])

    def test_from_person_phrase_finds_conversation_without_text_terms(self) -> None:
        baker.make("dashboard.DirectMessage", sender=self.alice, recipient=self.bob, body="hey there")
        hits = search_direct_messages(self.bob, f"messages from {self.alice.username}")
        self.assertEqual(len(hits), 1)

    def test_date_range_phrase_filters_by_created(self) -> None:
        # An explicit year, not a relative phrase like "last week", so this
        # doesn't depend on which day of the week the test happens to run.
        in_range = baker.make("dashboard.DirectMessage", sender=self.alice, recipient=self.bob, body="reunion photos")
        out_of_range = baker.make("dashboard.DirectMessage", sender=self.alice, recipient=self.bob, body="reunion photos too")
        type(in_range).objects.filter(pk=in_range.pk).update(created=datetime(2024, 6, 15, tzinfo=UTC))
        type(out_of_range).objects.filter(pk=out_of_range.pk).update(created=datetime(2020, 6, 15, tzinfo=UTC))
        hits = search_direct_messages(self.bob, "reunion 2024")
        self.assertEqual([hit["message"].pk for hit in hits], [in_range.pk])

    def test_result_url_anchors_to_the_message(self) -> None:
        message = baker.make("dashboard.DirectMessage", sender=self.alice, recipient=self.bob, body="asylum gate photos")
        hits = search_direct_messages(self.bob, "asylum gate")
        expected = reverse("messages.conversation", kwargs={"profile_slug": self.alice.ensure_slug()})
        self.assertEqual(hits[0]["url"], f"{expected}#dm-msg-{message.pk}")


class MessageSearchEndpointTests(TestCase):
    """HTTP endpoints backing the thread-header and sidebar search boxes."""

    def setUp(self) -> None:
        super().setUp()
        self.me = baker.make("auth.User", username="me").profile
        self.partner = baker.make("auth.User", username="partner").profile
        self.client.force_login(self.me.user)
        baker.make("dashboard.DirectMessage", sender=self.partner, recipient=self.me, body="Found the old asylum gate today")

    def test_conversation_search_requires_login(self) -> None:
        self.client.logout()
        response = self.client.get(reverse("messages.conversation_search", kwargs={"profile_slug": self.partner.slug}), {"q": "asylum"})
        self.assertEqual(response.status_code, 302)

    def test_conversation_search_finds_message(self) -> None:
        response = self.client.get(reverse("messages.conversation_search", kwargs={"profile_slug": self.partner.slug}), {"q": "asylum"})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "asylum")

    def test_conversation_search_below_min_length_returns_no_hits(self) -> None:
        response = self.client.get(reverse("messages.conversation_search", kwargs={"profile_slug": self.partner.slug}), {"q": "a"})
        self.assertNotContains(response, "dm-search-result")

    def test_messages_search_requires_login(self) -> None:
        self.client.logout()
        response = self.client.get(reverse("messages.search"), {"q": "asylum"})
        self.assertEqual(response.status_code, 302)

    def test_messages_search_finds_message_across_conversations(self) -> None:
        other_partner = baker.make("auth.User", username="other").profile
        baker.make("dashboard.DirectMessage", sender=other_partner, recipient=self.me, body="Unrelated chat")
        response = self.client.get(reverse("messages.search"), {"q": "asylum"})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "asylum")
        self.assertNotContains(response, "Unrelated")
