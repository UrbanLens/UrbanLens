"""Tests for AI link extraction: registry parsing/sanitization, the untrusted-input
allowlist, availability gating, the daily limit, the endpoints, and the pipeline.

The AI itself is always mocked - what's under test is everything around it: the
deterministic parse of its output, the strict per-field sanitization, the
never-overwrite apply rules, and the security posture (only allowlisted keys can
ever touch the pin, no matter what the model returns).
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from unittest.mock import patch

from django.urls import reverse
from hypothesis import given, settings as hypothesis_settings, strategies as st
from model_bakery import baker

from urbanlens.core.tests.testcase import SimpleTestCase, TestCase
from urbanlens.dashboard.models.aliases.model import PinAlias
from urbanlens.dashboard.models.link_extraction.model import LinkExtraction, LinkExtractionStatus
from urbanlens.dashboard.models.notifications.meta import NotificationType
from urbanlens.dashboard.models.notifications.model import NotificationLog
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.models.property_owner import PinOwner, PinPropertySale
from urbanlens.dashboard.models.site_settings import SiteSettings
from urbanlens.dashboard.models.subscriptions.model import SiteFeature
from urbanlens.dashboard.services.ai.link_extraction import (
    EXTRACTABLE_FIELDS,
    LinkExtractionError,
    _html_to_text,
    _parse_date,
    _parse_price,
    _validate_extraction_url,
    ai_extract_button_context,
    apply_extracted_fields,
    extractions_remaining_today,
    link_extraction_available,
    parse_ai_response,
    recently_requested_urls,
    run_extraction,
    start_link_extraction,
)


def _grant_ai_to_everyone() -> None:
    settings_obj = SiteSettings.get_current()
    SiteSettings.objects.filter(pk=settings_obj.pk).update(default_features=SiteFeature.AI)


class ParseHelpersTests(SimpleTestCase):
    """Deterministic parsing/sanitization of untrusted AI values (no DB writes)."""

    def test_parse_date_accepts_iso_and_bare_year(self) -> None:
        self.assertEqual(_parse_date("1987-06-15"), date(1987, 6, 15))
        self.assertEqual(_parse_date("1987"), date(1987, 1, 1))

    def test_parse_date_rejects_garbage_and_out_of_range(self) -> None:
        for bad in ("soon", "12/25/1987", "1200", "3050", "", None, ["1987"]):
            with self.assertRaises(ValueError):
                _parse_date(bad)

    def test_parse_price_tolerates_currency_punctuation(self) -> None:
        self.assertEqual(_parse_price("$1,250,000"), Decimal("1250000.00"))
        self.assertEqual(_parse_price(50000), Decimal("50000.00"))

    def test_parse_price_rejects_garbage(self) -> None:
        for bad in ("free", "-5", "NaN", "Infinity", None, True):
            with self.assertRaises(ValueError):
                _parse_price(bad)

    def test_parse_ai_response_handles_fences_and_prose(self) -> None:
        self.assertEqual(parse_ai_response('{"a": 1}'), {"a": 1})
        self.assertEqual(parse_ai_response('```json\n{"a": 1}\n```'), {"a": 1})
        self.assertEqual(parse_ai_response('Sure! Here you go: {"a": 1} hope that helps'), {"a": 1})
        self.assertEqual(parse_ai_response("no json here"), {})
        self.assertEqual(parse_ai_response('["a", "list"]'), {})
        self.assertEqual(parse_ai_response(""), {})

    @given(st.text(max_size=200))
    @hypothesis_settings(max_examples=50, deadline=None)
    def test_parse_ai_response_never_raises(self, answer: str) -> None:
        result = parse_ai_response(answer)
        self.assertIsInstance(result, dict)

    def test_html_to_text_strips_scripts_and_tags(self) -> None:
        markup = "<html><script>evil()</script><style>.x{}</style><h1>Old&nbsp;Mill</h1><p>Built long ago.</p></html>"
        text = _html_to_text(markup)
        self.assertIn("Old", text)
        self.assertIn("Built long ago.", text)
        self.assertNotIn("evil", text)
        self.assertNotIn("<", text)

    def test_validate_url_rejects_non_http_and_private_hosts(self) -> None:
        self.assertEqual(_validate_extraction_url("https://example.com/page"), "https://example.com/page")
        for bad in ("ftp://example.com", "javascript:alert(1)", "http://localhost/x", "http://127.0.0.1/x", "http://192.168.1.1/x", "http://[::1]/x", "", "https://" + "a" * 2100):
            with self.assertRaises(LinkExtractionError):
                _validate_extraction_url(bad)


class ApplyExtractedFieldsTests(TestCase):
    """The allowlisted, never-overwrite apply pipeline against real models."""

    def setUp(self) -> None:
        self.profile = Profile.objects.get(user=baker.make("auth.User"))
        self.pin = baker.make(Pin, profile=self.profile, name="Old Mill", name_is_user_provided=True)

    def test_full_payload_applies_every_field(self) -> None:
        payload = {
            "date_built": "1912",
            "date_abandoned": "1998-04-01",
            "owner_name": "Jordan Doe",
            "owner_company": "Doe Holdings LLC",
            "sale_date": "2015-09-30",
            "sale_price": "$425,000",
            "aliases": ["The Grand Mill", "Miller & Sons Works"],
        }
        results = apply_extracted_fields(self.pin, payload)

        self.pin.refresh_from_db()
        self.assertEqual(self.pin.date_built, date(1912, 1, 1))
        self.assertEqual(self.pin.date_abandoned, date(1998, 4, 1))
        owner = PinOwner.objects.get(pin=self.pin)
        self.assertEqual(owner.name, "Jordan Doe")
        self.assertEqual(owner.company_name, "Doe Holdings LLC")
        sale = PinPropertySale.objects.get(pin=self.pin)
        self.assertEqual(sale.sale_date, date(2015, 9, 30))
        self.assertEqual(sale.sale_price, Decimal("425000.00"))
        # Pin.save() itself maintains an alias row for the pin's own name, so
        # assert on the specific extracted names rather than a raw count.
        alias_names = set(PinAlias.objects.filter(pin=self.pin).values_list("name", flat=True))
        self.assertIn("The Grand Mill", alias_names)
        self.assertIn("Miller & Sons Works", alias_names)
        self.assertTrue(all(row["applied"] for row in results))

    def test_unknown_keys_are_ignored_entirely(self) -> None:
        """The allowlist in action: hostile keys can't reach the pin at all."""
        payload = {
            "name": "HACKED",
            "profile_id": 999,
            "is_superuser": True,
            "description": "injected",
            "latitude": "0",
        }
        results = apply_extracted_fields(self.pin, payload)
        self.assertEqual(results, [])
        self.pin.refresh_from_db()
        self.assertEqual(self.pin.name, "Old Mill")

    def test_existing_date_is_never_overwritten(self) -> None:
        self.pin.date_abandoned = date(1980, 1, 1)
        self.pin.save(update_fields=["date_abandoned"])
        results = apply_extracted_fields(self.pin, {"date_abandoned": "1999-01-01"})
        self.pin.refresh_from_db()
        self.assertEqual(self.pin.date_abandoned, date(1980, 1, 1))
        self.assertFalse(results[0]["applied"])
        self.assertIn("already set", results[0]["note"])

    def test_existing_date_built_is_never_overwritten(self) -> None:
        self.pin.date_built = date(1905, 6, 1)
        self.pin.save(update_fields=["date_built"])
        results = apply_extracted_fields(self.pin, {"date_built": "1950-01-01"})
        self.pin.refresh_from_db()
        self.assertEqual(self.pin.date_built, date(1905, 6, 1))
        self.assertFalse(results[0]["applied"])
        self.assertIn("already set", results[0]["note"])

    def test_rejected_values_are_recorded_not_applied(self) -> None:
        results = apply_extracted_fields(self.pin, {"date_abandoned": "not a date", "sale_price": "priceless"})
        self.assertEqual(len(results), 2)
        self.assertFalse(any(row["applied"] for row in results))
        self.assertTrue(all(row["note"].startswith("Rejected") for row in results))
        self.pin.refresh_from_db()
        self.assertIsNone(self.pin.date_abandoned)
        self.assertFalse(PinPropertySale.objects.exists())

    def test_duplicate_owner_and_aliases_are_skipped(self) -> None:
        PinOwner.objects.create(pin=self.pin, name="Jordan Doe")
        PinAlias.objects.create(pin=self.pin, name="The Grand Mill")
        alias_count_before = PinAlias.objects.filter(pin=self.pin).count()
        results = apply_extracted_fields(self.pin, {"owner_name": "jordan doe", "aliases": ["The Grand Mill"]})
        self.assertFalse(any(row["applied"] for row in results))
        self.assertEqual(PinOwner.objects.filter(pin=self.pin).count(), 1)
        self.assertEqual(PinAlias.objects.filter(pin=self.pin).count(), alias_count_before)

    def test_sale_price_without_date_still_records_a_sale(self) -> None:
        apply_extracted_fields(self.pin, {"sale_price": "99000"})
        sale = PinPropertySale.objects.get(pin=self.pin)
        self.assertEqual(sale.sale_price, Decimal("99000.00"))
        self.assertIsNone(sale.sale_date)

    def test_alias_list_is_sanitized_and_capped(self) -> None:
        payload = {"aliases": ["<script>x</script>Real Name", "", "unknown", *[f"Alias {i}" for i in range(20)]]}
        apply_extracted_fields(self.pin, payload)
        # Exclude the row Pin.save() maintains for the pin's own name.
        names = set(PinAlias.objects.filter(pin=self.pin).exclude(name=self.pin.name).values_list("name", flat=True))
        self.assertTrue(all("<" not in name for name in names))
        # Capped at 10 candidates before filtering, so never more than 10 rows.
        self.assertLessEqual(len(names), 10)

    def test_registry_keys_are_unique(self) -> None:
        keys = [field.key for field in EXTRACTABLE_FIELDS]
        self.assertEqual(len(keys), len(set(keys)))


class AvailabilityAndLimitTests(TestCase):
    """Feature gating and the admin-settable per-user daily limit."""

    def setUp(self) -> None:
        baker.make("auth.User")  # bootstrap admin
        self.user = baker.make("auth.User")
        self.profile = Profile.objects.get(user=self.user)
        self.pin = baker.make(Pin, profile=self.profile)

    def test_unavailable_without_subscription_feature(self) -> None:
        self.assertFalse(link_extraction_available(self.user, self.profile))

    def test_available_with_feature_and_toggles_on(self) -> None:
        _grant_ai_to_everyone()
        self.assertTrue(link_extraction_available(self.user, self.profile))

    def test_profile_master_toggle_disables(self) -> None:
        _grant_ai_to_everyone()
        Profile.objects.filter(pk=self.profile.pk).update(ai_enabled=False)
        self.profile.refresh_from_db()
        self.assertFalse(link_extraction_available(self.user, self.profile))

    def test_site_switch_disables(self) -> None:
        _grant_ai_to_everyone()
        SiteSettings.objects.filter(pk=SiteSettings.get_current().pk).update(ai_link_extraction_enabled=False)
        self.assertFalse(link_extraction_available(self.user, self.profile))

    def test_daily_limit_counts_down_and_blocks(self) -> None:
        _grant_ai_to_everyone()
        SiteSettings.objects.filter(pk=SiteSettings.get_current().pk).update(ai_link_extraction_daily_limit=2)
        self.assertEqual(extractions_remaining_today(self.profile), 2)
        with patch("urbanlens.dashboard.services.celery.safely_enqueue_task", return_value=object()):
            start_link_extraction(self.user, self.profile, self.pin, "https://example.com/a")
            self.assertEqual(extractions_remaining_today(self.profile), 1)
            start_link_extraction(self.user, self.profile, self.pin, "https://example.com/b")
            self.assertEqual(extractions_remaining_today(self.profile), 0)
            with self.assertRaises(LinkExtractionError):
                start_link_extraction(self.user, self.profile, self.pin, "https://example.com/c")

    def test_start_rejects_bad_url_without_consuming_limit(self) -> None:
        _grant_ai_to_everyone()
        with self.assertRaises(LinkExtractionError):
            start_link_extraction(self.user, self.profile, self.pin, "http://127.0.0.1/internal")
        self.assertFalse(LinkExtraction.objects.exists())

    def test_start_marks_failed_when_broker_unavailable(self) -> None:
        _grant_ai_to_everyone()
        with patch("urbanlens.dashboard.services.celery.safely_enqueue_task", return_value=None):
            extraction = start_link_extraction(self.user, self.profile, self.pin, "https://example.com/a")
        self.assertEqual(extraction.status, LinkExtractionStatus.FAILED)


class _FakeGateway:
    def __init__(self, answer: str | None):
        self._answer = answer

    def send_prompt(self, prompt: str, **kwargs) -> str | None:
        return self._answer


class RunExtractionPipelineTests(TestCase):
    """The Celery-side pipeline with the fetch and the AI mocked."""

    def setUp(self) -> None:
        baker.make("auth.User")
        self.user = baker.make("auth.User")
        self.profile = Profile.objects.get(user=self.user)
        self.pin = baker.make(Pin, profile=self.profile, name="Old Mill", name_is_user_provided=True)
        self.extraction = LinkExtraction.objects.create(profile=self.profile, pin=self.pin, url="https://example.com/history")
        _grant_ai_to_everyone()

    def _run(self, page_text: str | None = "Some page text", answer: str | None = "{}") -> LinkExtraction:
        fetch_patch = (
            patch("urbanlens.dashboard.services.ai.link_extraction.fetch_page_text", return_value=page_text)
            if page_text is not None
            else patch("urbanlens.dashboard.services.ai.link_extraction.fetch_page_text", side_effect=LinkExtractionError("The page couldn't be fetched."))
        )
        with fetch_patch, patch("urbanlens.dashboard.services.ai.factory.get_gateway", return_value=_FakeGateway(answer)):
            run_extraction(self.extraction)
        self.extraction.refresh_from_db()
        return self.extraction

    def test_successful_run_applies_and_notifies(self) -> None:
        extraction = self._run(answer='{"date_abandoned": "1998", "aliases": ["Grand Mill"]}')
        self.assertEqual(extraction.status, LinkExtractionStatus.SUCCESS)
        self.pin.refresh_from_db()
        self.assertEqual(self.pin.date_abandoned, date(1998, 1, 1))
        self.assertTrue(PinAlias.objects.filter(pin=self.pin, name="Grand Mill").exists())
        notification = NotificationLog.objects.get(profile=self.profile, notification_type=NotificationType.AI_EXTRACTION)
        self.assertEqual(notification.url, reverse("ai.extractions"))

    def test_empty_answer_is_recorded_as_empty(self) -> None:
        extraction = self._run(answer='{"date_abandoned": null, "owner_name": null}')
        self.assertEqual(extraction.status, LinkExtractionStatus.EMPTY)
        self.assertTrue(NotificationLog.objects.filter(profile=self.profile, notification_type=NotificationType.AI_EXTRACTION).exists())

    def test_fetch_failure_is_recorded_and_notifies(self) -> None:
        extraction = self._run(page_text=None)
        self.assertEqual(extraction.status, LinkExtractionStatus.FAILED)
        self.assertIn("couldn't be fetched", extraction.error)
        self.assertTrue(NotificationLog.objects.filter(profile=self.profile, notification_type=NotificationType.AI_EXTRACTION).exists())

    def test_prompt_injection_keys_cannot_touch_the_pin(self) -> None:
        extraction = self._run(answer='{"name": "HACKED", "profile": 1, "date_abandoned": "1998"}')
        self.assertEqual(extraction.status, LinkExtractionStatus.SUCCESS)
        self.pin.refresh_from_db()
        self.assertEqual(self.pin.name, "Old Mill")
        self.assertEqual(self.pin.date_abandoned, date(1998, 1, 1))
        recorded_keys = {row["key"] for row in extraction.results_rows}
        self.assertEqual(recorded_keys, {"date_abandoned"})


class RecentlyRequestedUrlsTests(TestCase):
    """The per-link one-week cooldown: recently_requested_urls / ai_extract_button_context."""

    def setUp(self) -> None:
        baker.make("auth.User")
        self.user = baker.make("auth.User")
        self.profile = Profile.objects.get(user=self.user)
        self.pin = baker.make(Pin, profile=self.profile, name="Old Mill", name_is_user_provided=True)

    def test_no_extractions_means_empty_set(self) -> None:
        self.assertEqual(recently_requested_urls(self.pin), frozenset())

    def test_recent_extraction_is_in_the_set(self) -> None:
        LinkExtraction.objects.create(profile=self.profile, pin=self.pin, url="https://example.com/history")
        self.assertEqual(recently_requested_urls(self.pin), frozenset({"https://example.com/history"}))

    def test_other_links_on_the_same_pin_are_unaffected(self) -> None:
        LinkExtraction.objects.create(profile=self.profile, pin=self.pin, url="https://example.com/a")
        recent = recently_requested_urls(self.pin)
        self.assertIn("https://example.com/a", recent)
        self.assertNotIn("https://example.com/b", recent)

    def test_extraction_older_than_the_cooldown_is_excluded(self) -> None:
        from datetime import timedelta

        from django.utils import timezone

        extraction = LinkExtraction.objects.create(profile=self.profile, pin=self.pin, url="https://example.com/old")
        LinkExtraction.objects.filter(pk=extraction.pk).update(created=timezone.now() - timedelta(days=8))
        self.assertEqual(recently_requested_urls(self.pin), frozenset())

    def test_another_pins_extraction_does_not_leak_in(self) -> None:
        other_pin = baker.make(Pin, profile=self.profile)
        LinkExtraction.objects.create(profile=self.profile, pin=other_pin, url="https://example.com/x")
        self.assertEqual(recently_requested_urls(self.pin), frozenset())

    def test_button_context_empty_set_when_unavailable(self) -> None:
        LinkExtraction.objects.create(profile=self.profile, pin=self.pin, url="https://example.com/a")
        context = ai_extract_button_context(self.user, self.profile, self.pin)
        self.assertFalse(context["can_ai_extract"])
        self.assertEqual(context["recently_extracted_urls"], frozenset())

    def test_button_context_populated_when_available(self) -> None:
        _grant_ai_to_everyone()
        LinkExtraction.objects.create(profile=self.profile, pin=self.pin, url="https://example.com/a")
        context = ai_extract_button_context(self.user, self.profile, self.pin)
        self.assertTrue(context["can_ai_extract"])
        self.assertEqual(context["recently_extracted_urls"], frozenset({"https://example.com/a"}))


class RecentlyRequestedButtonRenderingTests(TestCase):
    """The button itself hides only for the specific link just requested."""

    def setUp(self) -> None:
        baker.make("auth.User")
        self.user = baker.make("auth.User")
        self.profile = Profile.objects.get(user=self.user)
        self.pin = baker.make(Pin, profile=self.profile, name="Old Mill", name_is_user_provided=True)
        self.client.force_login(self.user)
        _grant_ai_to_everyone()

    def test_button_disappears_only_for_the_requested_link(self) -> None:
        from urbanlens.dashboard.models.links.model import PinLink

        PinLink.objects.create(pin=self.pin, url="https://example.com/requested")
        PinLink.objects.create(pin=self.pin, url="https://example.com/other")
        LinkExtraction.objects.create(profile=self.profile, pin=self.pin, url="https://example.com/requested")

        response = self.client.get(reverse("pin.links", args=[self.pin.slug]))
        self.assertEqual(response.status_code, 200)
        content = response.content.decode()
        # Each link chip is its own <span> block in source order - split on the
        # chip boundary so each assertion only looks at that link's own markup.
        requested_chip, other_chip = content.split('<span class="pin-link-chip">')[1:3]
        self.assertNotIn("ai-extract-btn", requested_chip)
        self.assertIn("ai-extract-btn", other_chip)

    def test_extract_endpoint_response_hides_the_button_for_that_link_on_next_render(self) -> None:
        with patch("urbanlens.dashboard.services.celery.safely_enqueue_task", return_value=object()):
            self.client.post(reverse("pin.ai_extract", args=[self.pin.slug]), {"url": "https://example.com/history"})
        context = ai_extract_button_context(self.user, self.profile, self.pin)
        self.assertIn("https://example.com/history", context["recently_extracted_urls"])


class EndpointTests(TestCase):
    """The start endpoint and the (unlinked) review page."""

    def setUp(self) -> None:
        baker.make("auth.User")
        self.user = baker.make("auth.User")
        self.profile = Profile.objects.get(user=self.user)
        self.pin = baker.make(Pin, profile=self.profile, name="Old Mill", name_is_user_provided=True)
        self.client.force_login(self.user)

    def test_extract_endpoint_queues_a_run(self) -> None:
        _grant_ai_to_everyone()
        with patch("urbanlens.dashboard.services.celery.safely_enqueue_task", return_value=object()):
            response = self.client.post(reverse("pin.ai_extract", args=[self.pin.slug]), {"url": "https://example.com/history"})
        self.assertEqual(response.status_code, 200)
        extraction = LinkExtraction.objects.get()
        self.assertEqual(extraction.pin, self.pin)
        self.assertEqual(extraction.url, "https://example.com/history")

    def test_extract_endpoint_403s_without_feature(self) -> None:
        response = self.client.post(reverse("pin.ai_extract", args=[self.pin.slug]), {"url": "https://example.com/history"})
        self.assertEqual(response.status_code, 403)
        self.assertFalse(LinkExtraction.objects.exists())

    def test_extract_endpoint_404s_for_someone_elses_pin(self) -> None:
        _grant_ai_to_everyone()
        other_pin = baker.make(Pin, profile=baker.make("auth.User").profile)
        response = self.client.post(reverse("pin.ai_extract", args=[other_pin.slug]), {"url": "https://example.com/x"})
        self.assertEqual(response.status_code, 404)

    def test_review_page_lists_only_own_runs(self) -> None:
        mine = LinkExtraction.objects.create(profile=self.profile, pin=self.pin, url="https://example.com/mine", status=LinkExtractionStatus.SUCCESS, results=[{"key": "date_abandoned", "label": "Date abandoned", "value": "1998-01-01", "applied": True, "note": "Set on the pin."}])
        other_profile = Profile.objects.get(user=baker.make("auth.User"))
        other_pin = baker.make(Pin, profile=other_profile)
        LinkExtraction.objects.create(profile=other_profile, pin=other_pin, url="https://example.com/theirs")

        response = self.client.get(reverse("ai.extractions"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, mine.url)
        self.assertNotContains(response, "https://example.com/theirs")
        self.assertContains(response, "Date abandoned")

    def test_review_page_requires_login(self) -> None:
        self.client.logout()
        response = self.client.get(reverse("ai.extractions"))
        self.assertEqual(response.status_code, 302)
