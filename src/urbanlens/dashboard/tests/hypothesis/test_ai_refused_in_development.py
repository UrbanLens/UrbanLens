"""Each AI feature degrades to "not available in this environment" where hosted AI is not called (D26).

In development and local nothing reaches Cloudflare, OpenAI or Anthropic. These cover what every feature does
instead: it says so, makes no call, writes no ledger row, caches nothing, and marks no work as tried, so the same
work is done the moment an override (or production) lets it.
"""

from __future__ import annotations

from collections.abc import Iterator
import contextlib
from decimal import Decimal
from unittest import mock

from django.core.cache import cache
from django.test import override_settings
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.api_call_log.model import ApiCallLog
from urbanlens.dashboard.models.images import ImageKeyword
from urbanlens.dashboard.models.link_extraction.model import LinkExtraction, LinkExtractionStatus
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.models.site_settings.model import SiteSettings
from urbanlens.dashboard.models.subscriptions import SiteFeature
from urbanlens.dashboard.models.trips.model import Trip, TripMembership
from urbanlens.dashboard.models.trivia.model import (
    TriviaGenerationAttempt,
    TriviaQuestion,
    TriviaQuestionSource,
    TriviaQuestionStatus,
    TriviaQuestionVote,
    TriviaQuestionVoteKind,
)
from urbanlens.dashboard.models.wiki.model import Wiki
from urbanlens.dashboard.services.ai.access import assistant_available
from urbanlens.dashboard.services.core.rate_limiter import EnvironmentRefusedError

HERE = "not available in this environment"
DEVELOPMENT = "development"


@contextlib.contextmanager
def deployment(environment: str, *, overrides: dict[str, float] | None = None) -> Iterator[None]:
    """Pretend to be one deployment: its ``UL_ENVIRONMENT`` and ``UL_ENVIRONMENT_SHARE_OVERRIDES``."""
    with (
        override_settings(ENVIRONMENT_NAME=environment, TESTING=False),
        mock.patch("urbanlens.UrbanLens.settings.app.settings.environment_share", None),
        mock.patch("urbanlens.UrbanLens.settings.app.settings.environment_share_overrides", overrides or {}),
    ):
        yield


class _Gateway:
    """An LLM gateway that counts the calls it is asked to make and answers from a script."""

    model = "fake-model"
    tokens = 0

    def __init__(self, answer: str | None = "", answers: list[str] | None = None) -> None:
        self.answer = answer
        self.answers = answers or []
        self.calls = 0
        self.cost = Decimal(0)

    def send_prompt(self, prompt: str, **kwargs) -> str | None:
        self.calls += 1
        return self.answer

    def send_prompt_list(self, prompt: str, *, max_results: int | None = None, **kwargs) -> list[str]:
        self.calls += 1
        return self.answers

    def send_with_tools(self, prompt: str, tools: list, *, timeout: float | None = None):
        self.calls += 1


def _grant(*features: SiteFeature) -> None:
    """Give every account the features, so only the environment stands between it and the call."""
    SiteSettings.objects.filter(pk=SiteSettings.get_current().pk).update(
        default_features=",".join(feature.value for feature in features)
    )


def _member() -> Profile:
    return Profile.objects.get(user=baker.make("auth.User"))


class _AiTestCase(TestCase):
    def setUp(self) -> None:
        super().setUp()
        baker.make("auth.User")  # the first account is promoted to site admin, who holds every feature
        _grant(SiteFeature.AI, SiteFeature.AI_PHOTO_PROCESSING)
        self.profile = _member()
        self.user = self.profile.user
        cache.clear()

    def assertNothingWasAsked(self, *gateways: _Gateway) -> None:
        for gateway in gateways:
            self.assertEqual(gateway.calls, 0, "a hosted provider was called")
        self.assertFalse(ApiCallLog.objects.exists(), "a refused call left a ledger row")


class AssistantTests(_AiTestCase):
    def _available(self, environment: str, **kwargs) -> bool:
        with override_settings(UL_AI_WORKER_ENABLED=True), deployment(environment, **kwargs):
            return assistant_available(self.profile)

    def test_it_is_unavailable_in_development_and_nowhere_else(self) -> None:
        self.assertFalse(self._available("development"))
        self.assertFalse(self._available("local"))
        self.assertTrue(self._available("staging"))
        self.assertTrue(self._available("production"))

    def test_an_override_turns_it_back_on(self) -> None:
        self.assertTrue(self._available("development", overrides={"assistant": 1.0}))
        self.assertTrue(self._available("development", overrides={"ai_anthropic": 1.0}))
        self.assertFalse(self._available("development", overrides={"link_extraction": 1.0}))

    def test_the_page_and_overlay_say_so_and_do_not_send_the_user_to_settings(self) -> None:
        self.client.force_login(self.user)
        with override_settings(UL_AI_WORKER_ENABLED=True), deployment(DEVELOPMENT):
            for name in ("assistant", "assistant.overlay"):
                with self.subTest(page=name):
                    response = self.client.get(reverse(name))
                    self.assertEqual(response.status_code, 200)
                    self.assertIn(HERE, response.content.decode().lower())
                    self.assertNotIn("ai-settings-section", response.content.decode())

    def test_a_message_gets_that_answer_and_queues_nothing(self) -> None:
        self.client.force_login(self.user)
        with (
            override_settings(UL_AI_WORKER_ENABLED=True),
            deployment(DEVELOPMENT),
            mock.patch("urbanlens.dashboard.controllers.assistant.safely_enqueue_task") as enqueue,
        ):
            response = self.client.post(reverse("assistant.message"), {"message": "find my pins"})
        self.assertEqual(response.status_code, 200)
        enqueue.assert_not_called()
        self.assertIn(HERE, response.content.decode().lower())

    def test_a_turn_already_queued_says_so_and_calls_nothing(self) -> None:
        from urbanlens.dashboard.services.ai.tasks import run_assistant_turn_task
        from urbanlens.dashboard.services.ai.turns import acquire_turn_lock

        token = acquire_turn_lock(self.profile)
        assert token is not None
        with override_settings(UL_AI_WORKER_ENABLED=True), deployment(DEVELOPMENT):
            result = run_assistant_turn_task(self.profile.pk, [], "hello", token)
        self.assertIn(HERE, result["reply"].lower())
        self.assertFalse(ApiCallLog.objects.exists())

    def test_a_turn_that_gets_as_far_as_the_provider_round_says_so_instead_of_apologising(self) -> None:
        from urbanlens.dashboard.services.ai.assistant import run_assistant_turn

        gateway = _Gateway()
        with (
            deployment(DEVELOPMENT),
            mock.patch("urbanlens.dashboard.services.ai.assistant.get_gateway", return_value=gateway),
        ):
            turn = run_assistant_turn(self.profile, [], "hello")
        self.assertIn(HERE, turn.reply.lower())
        self.assertNothingWasAsked(gateway)


class TriviaTests(_AiTestCase):
    def _wiki(self) -> Wiki:
        from urbanlens.dashboard.services.trivia.generation import MIN_DESCRIPTION_LENGTH

        return baker.make(Wiki, location=baker.make(Location), description="x" * MIN_DESCRIPTION_LENGTH)

    def test_a_submitted_question_stays_pending_rather_than_rejected(self) -> None:
        from urbanlens.dashboard.services.trivia import classifier
        from urbanlens.dashboard.services.trivia.submission import classify_and_update

        question = baker.make(
            TriviaQuestion,
            location=baker.make(Location),
            source=TriviaQuestionSource.USER_SUBMITTED,
            status=TriviaQuestionStatus.PENDING_REVIEW,
            submitted_by=self.profile,
            prompt="What year was it built?",
            answer="1937",
        )
        gateway = _Gateway("APPROVE")
        with deployment(DEVELOPMENT), mock.patch.object(classifier, "get_gateway", return_value=gateway):
            classify_and_update(question)
        question.refresh_from_db()
        self.assertEqual(question.status, TriviaQuestionStatus.PENDING_REVIEW)
        self.assertFalse(question.rejection_reason)
        self.assertNothingWasAsked(gateway)

    def test_an_answer_check_judges_no_match_without_asking(self) -> None:
        from urbanlens.dashboard.services.trivia import answer_check

        gateway = _Gateway("MATCH")
        with deployment(DEVELOPMENT), mock.patch.object(answer_check, "get_gateway", return_value=gateway):
            self.assertFalse(answer_check.is_answer_equivalent("nineteen thirty seven", "1937", profile=self.profile))
        self.assertNothingWasAsked(gateway)

    def test_the_generation_sweep_marks_no_wiki_tried(self) -> None:
        from urbanlens.dashboard.services.trivia import generation

        for _ in range(3):
            self._wiki()
        gateway = _Gateway(answers=["When was it built?|||1937"])
        with deployment(DEVELOPMENT), mock.patch.object(generation, "_gateway", return_value=gateway):
            summary = generation.sweep_wikis_for_generation(batch_size=5)
        self.assertEqual(summary, {"wikis_considered": 0, "questions_created": 0})
        self.assertFalse(TriviaGenerationAttempt.objects.exists())
        self.assertNothingWasAsked(gateway)

    def test_a_sweep_that_cannot_moderate_does_not_spend_a_hosted_call_to_throw_the_answer_away(self) -> None:
        """Generation was opted in and moderation was not: nothing is generated, so nothing is wasted or marked tried."""
        from urbanlens.dashboard.services.trivia import classifier, generation

        wikis = [self._wiki() for _ in range(2)]
        generator = _Gateway(answers=["When was it built?|||1937"])
        moderator = _Gateway("APPROVE")
        with (
            deployment(DEVELOPMENT, overrides={"trivia_generation": 1.0}),
            mock.patch.object(generation, "_gateway", return_value=generator),
            mock.patch.object(classifier, "get_gateway", return_value=moderator),
        ):
            summary = generation.sweep_wikis_for_generation(batch_size=5)
        self.assertEqual(summary, {"wikis_considered": 0, "questions_created": 0})
        self.assertFalse(TriviaGenerationAttempt.objects.exists())
        self.assertFalse(TriviaQuestion.objects.filter(location__in=[wiki.location for wiki in wikis]).exists())
        self.assertEqual((generator.calls, moderator.calls), (0, 0))

    def test_a_refused_moderation_stops_generation_and_marks_nothing(self) -> None:
        """The check up front cannot see a refusal that arrives mid-run (a provider opted out, a limit reached)."""
        from urbanlens.dashboard.services.trivia import classifier, generation

        wiki = self._wiki()
        generator = _Gateway(answers=["When was it built?|||1937"])
        with (
            deployment(DEVELOPMENT, overrides={"trivia_generation": 1.0}),
            mock.patch.object(classifier, "get_gateway", return_value=_Gateway("APPROVE")),
            self.assertRaises(EnvironmentRefusedError),
        ):
            generation.generate_questions_for_wiki(wiki, gateway=generator, raise_refusal=True)
        self.assertFalse(TriviaQuestion.objects.filter(location=wiki.location).exists())

    def test_the_moderation_refusal_is_still_an_ordinary_verdict_for_a_direct_caller(self) -> None:
        from urbanlens.dashboard.services.trivia import classifier

        with deployment(DEVELOPMENT), mock.patch.object(classifier, "get_gateway", return_value=_Gateway("APPROVE")):
            verdict = classifier.classify_trivia_question("When?", "1937", baker.make(Location))
        self.assertEqual((verdict.approved, verdict.reason), (False, "ai_unavailable"))
        with (
            deployment(DEVELOPMENT),
            mock.patch.object(classifier, "get_gateway", return_value=_Gateway("APPROVE")),
            self.assertRaises(EnvironmentRefusedError),
        ):
            classifier.classify_trivia_question("When?", "1937", baker.make(Location), raise_refusal=True)

    def _upvoted_question(self) -> TriviaQuestion:
        location = baker.make(Location)
        baker.make(Wiki, location=location)
        question = baker.make(
            TriviaQuestion,
            location=location,
            source=TriviaQuestionSource.USER_SUBMITTED,
            status=TriviaQuestionStatus.APPROVED,
            prompt="What year was it built?",
            answer="1937",
        )
        for _ in range(5):
            baker.make(TriviaQuestionVote, question=question, profile=_member(), kind=TriviaQuestionVoteKind.UPVOTE)
        return question

    def test_the_wiki_sweep_marks_no_question_processed(self) -> None:
        from urbanlens.dashboard.services.trivia import wiki_incorporation

        questions = [self._upvoted_question() for _ in range(3)]
        gateway = _Gateway("The mill opened in 1937.")
        with deployment(DEVELOPMENT), mock.patch.object(wiki_incorporation, "get_gateway", return_value=gateway):
            summary = wiki_incorporation.sweep_questions_for_wiki_incorporation(batch_size=5)
        self.assertEqual(summary, {"questions_considered": 0, "questions_incorporated": 0})
        self.assertNothingWasAsked(gateway)
        for question in questions:
            question.refresh_from_db()
            self.assertIsNone(question.wiki_incorporated_at)

    def test_a_sweep_that_cannot_review_does_not_draft(self) -> None:
        """The writer was opted in and the safety review was not: nothing is drafted, and no question is marked processed."""
        from urbanlens.dashboard.services.trivia import wiki_incorporation

        questions = [self._upvoted_question() for _ in range(2)]
        writer = _Gateway("The mill opened in 1937.")
        reviewer = _Gateway("APPROVE")
        with (
            deployment(DEVELOPMENT, overrides={"trivia_wiki_incorporation": 1.0}),
            mock.patch.object(wiki_incorporation, "get_gateway", return_value=writer),
            mock.patch("urbanlens.dashboard.services.ai.factory.get_gateway", return_value=reviewer),
        ):
            summary = wiki_incorporation.sweep_questions_for_wiki_incorporation(batch_size=5)
        self.assertEqual(summary, {"questions_considered": 0, "questions_incorporated": 0})
        self.assertEqual((writer.calls, reviewer.calls), (0, 0))
        for question in questions:
            question.refresh_from_db()
            self.assertIsNone(question.wiki_incorporated_at)

    def test_in_production_too_a_review_switched_off_keeps_the_questions_waiting(self) -> None:
        """Before, each was drafted and then marked processed on the strength of a review that never ran."""
        from urbanlens.dashboard.models.api_rate_limit.model import ApiRateLimit
        from urbanlens.dashboard.services.trivia import wiki_incorporation

        ApiRateLimit.objects.update_or_create(
            service="article_safety", defaults={"display_name": "s", "enabled": False}
        )
        questions = [self._upvoted_question() for _ in range(2)]
        writer = _Gateway("The mill opened in 1937.")
        with deployment("production"), mock.patch.object(wiki_incorporation, "get_gateway", return_value=writer):
            wiki_incorporation.sweep_questions_for_wiki_incorporation(batch_size=5)
        self.assertEqual(writer.calls, 0)
        for question in questions:
            question.refresh_from_db()
            self.assertIsNone(question.wiki_incorporated_at)

    def test_a_review_refused_mid_run_leaves_the_question_unprocessed(self) -> None:
        from urbanlens.dashboard.services.trivia import wiki_incorporation

        question = self._upvoted_question()
        with (
            deployment(DEVELOPMENT, overrides={"trivia_wiki_incorporation": 1.0}),
            mock.patch.object(wiki_incorporation, "get_gateway", return_value=_Gateway("The mill opened in 1937.")),
            mock.patch("urbanlens.dashboard.services.ai.factory.get_gateway", return_value=_Gateway("APPROVE")),
            self.assertRaises(EnvironmentRefusedError),
        ):
            wiki_incorporation.incorporate_question_into_wiki(question, raise_refusal=True)
        question.refresh_from_db()
        self.assertIsNone(question.wiki_incorporated_at)

    def test_a_safety_review_that_ran_and_refused_the_text_still_marks_it_processed(self) -> None:
        """Only a refusal before the call is "not tried"; a verdict of REJECT is an answer."""
        from urbanlens.dashboard.services.ai.article_safety import ArticleSafetyVerdict
        from urbanlens.dashboard.services.trivia import wiki_incorporation

        question = self._upvoted_question()
        with (
            mock.patch.object(wiki_incorporation, "get_gateway", return_value=_Gateway("The mill opened in 1937.")),
            mock.patch.object(
                wiki_incorporation,
                "classify_article_text",
                return_value=ArticleSafetyVerdict(approved=False, reason="off_topic"),
            ),
        ):
            wiki_incorporation.incorporate_question_into_wiki(question)
        question.refresh_from_db()
        self.assertIsNotNone(question.wiki_incorporated_at)


class LinkExtractionTests(_AiTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.pin = baker.make(Pin, profile=self.profile, name="Old Mill", name_is_user_provided=True)

    def test_the_buttons_do_not_exist_here(self) -> None:
        from urbanlens.dashboard.services.ai.link_extraction import link_extraction_available

        self.assertTrue(link_extraction_available(self.user, self.profile))
        with deployment(DEVELOPMENT):
            self.assertFalse(link_extraction_available(self.user, self.profile))
        with deployment(DEVELOPMENT, overrides={"link_extraction": 1.0}):
            self.assertTrue(link_extraction_available(self.user, self.profile))
        with deployment("staging"):
            self.assertTrue(link_extraction_available(self.user, self.profile))

    def test_starting_one_says_so_and_spends_none_of_the_day_s_allowance(self) -> None:
        from urbanlens.dashboard.services.ai.link_extraction import (
            LinkExtractionUnavailableHereError,
            extractions_remaining_today,
            start_link_extraction,
        )

        before = extractions_remaining_today(self.profile)
        with (
            deployment(DEVELOPMENT),
            mock.patch("urbanlens.dashboard.services.core.celery.safely_enqueue_task") as enqueue,
            self.assertRaises(LinkExtractionUnavailableHereError) as raised,
        ):
            start_link_extraction(self.user, self.profile, self.pin, "https://example.com/a")
        self.assertIn(HERE, str(raised.exception).lower())
        enqueue.assert_not_called()
        self.assertFalse(LinkExtraction.objects.exists())
        self.assertEqual(extractions_remaining_today(self.profile), before)

    def test_the_endpoint_toasts_it(self) -> None:
        self.client.force_login(self.user)
        with deployment(DEVELOPMENT):
            response = self.client.post(
                reverse("pin.ai_extract", args=[self.pin.slug]), {"url": "https://example.com/a"}
            )
        self.assertIn(HERE, response["HX-Trigger"].lower())
        self.assertFalse(LinkExtraction.objects.exists())

    def test_the_review_page_says_so(self) -> None:
        self.client.force_login(self.user)
        with deployment(DEVELOPMENT):
            response = self.client.get(reverse("ai.extractions"))
        self.assertIn(HERE, response.content.decode().lower())

    def test_a_run_queued_before_the_change_fails_politely_and_reads_no_page(self) -> None:
        from urbanlens.dashboard.services.ai import link_extraction

        extraction = LinkExtraction.objects.create(profile=self.profile, pin=self.pin, url="https://example.com/a")
        gateway = _Gateway("{}")
        with (
            deployment(DEVELOPMENT),
            mock.patch.object(link_extraction, "fetch_page_text") as fetch,
            mock.patch("urbanlens.dashboard.services.ai.factory.get_gateway", return_value=gateway),
        ):
            link_extraction.run_extraction(extraction)
        extraction.refresh_from_db()
        fetch.assert_not_called()
        self.assertEqual(extraction.status, LinkExtractionStatus.FAILED)
        self.assertIn(HERE, extraction.error.lower())
        self.assertNothingWasAsked(gateway)

    def test_article_expansion_says_so_when_only_the_reading_was_opted_in(self) -> None:
        from urbanlens.dashboard.services.ai.article_expansion import expand_articles_from_page

        extraction = LinkExtraction.objects.create(profile=self.profile, pin=self.pin, url="https://example.com/a")
        gateway = _Gateway("A paragraph about the mill.")
        with (
            deployment(DEVELOPMENT, overrides={"link_extraction": 1.0}),
            mock.patch("urbanlens.dashboard.services.ai.factory.get_gateway", return_value=gateway),
        ):
            rows = expand_articles_from_page(extraction, "Page text")
        self.assertTrue(rows)
        self.assertTrue(all(HERE in row["note"].lower() and not row["applied"] for row in rows))
        self.assertNothingWasAsked(gateway)


class DocumentImportTests(_AiTestCase):
    def test_it_is_unavailable_here(self) -> None:
        from urbanlens.dashboard.services.ai.document_import import ai_document_import_available

        self.assertTrue(ai_document_import_available(self.profile))
        with deployment(DEVELOPMENT):
            self.assertFalse(ai_document_import_available(self.profile))
        with deployment(DEVELOPMENT, overrides={"document_pin_import": 1.0}):
            self.assertTrue(ai_document_import_available(self.profile))

    def test_extraction_says_so_rather_than_reporting_no_pins_found(self) -> None:
        from urbanlens.dashboard.services.ai.document_import import extract_pins_from_text

        gateway = _Gateway("name,description,address,latitude,longitude")
        with (
            deployment(DEVELOPMENT, overrides={"document_pin_import": 0.0}),
            mock.patch("urbanlens.dashboard.services.ai.factory.get_gateway", return_value=gateway),
        ):
            found, warning = extract_pins_from_text("notes.txt", "Old Mill, 1 Main St", self.profile)
        self.assertIsNone(found)
        assert warning is not None
        self.assertIn(HERE, warning.lower())
        self.assertNothingWasAsked(gateway)

    def _preview(self, *uploads: tuple[str, bytes]) -> dict:
        """Upload, parse in the sandbox, then finish in development; the preview's final state."""
        from django.core.files.uploadedfile import SimpleUploadedFile

        from urbanlens.dashboard.services.core import single_flight
        from urbanlens.dashboard.services.pins import import_preview

        enqueue = "urbanlens.dashboard.services.core.celery.safely_enqueue_task"
        self.client.force_login(self.user)
        self.addCleanup(single_flight.release, import_preview.guard_key(self.profile.pk))
        files = [SimpleUploadedFile(name, data, content_type="application/octet-stream") for name, data in uploads]
        with (
            override_settings(UL_PROCESS_ROLE="web", UL_UNTRUSTED_PARSE_POLICY="deny"),
            mock.patch(enqueue, return_value=mock.Mock()),
        ):
            job = self.client.post(reverse("pin.import.preview"), {"upload_files": files}).json()
        with (
            override_settings(UL_PROCESS_ROLE="sandbox", UL_UNTRUSTED_PARSE_POLICY="deny"),
            mock.patch(enqueue, return_value=mock.Mock()),
        ):
            import_preview.parse_import_preview(self.profile.pk, job["job_id"])
        with (
            deployment(DEVELOPMENT),
            override_settings(UL_PROCESS_ROLE="worker", UL_UNTRUSTED_PARSE_POLICY="deny"),
            mock.patch("urbanlens.dashboard.services.ai.document_import.extract_pins_from_text") as extract,
        ):
            import_preview.finish_import_preview(self.profile.pk, job["job_id"])
        extract.assert_not_called()
        return self.client.get(job["status_url"]).json()

    def test_an_upload_of_documents_alone_says_why_nothing_came_of_it(self) -> None:
        state = self._preview(("notes.txt", b"The old mill on Route 9."))
        self.assertEqual(state["status"], "error", state)
        self.assertIn(HERE, state["message"].lower())

    def test_an_upload_with_pins_in_it_keeps_them_and_warns_about_the_documents(self) -> None:
        kml = (
            b'<?xml version="1.0" encoding="UTF-8"?><kml xmlns="http://www.opengis.net/kml/2.2"><Document><Placemark>'
            b"<name>Test Spot</name><Point><coordinates>-73.9251,41.7003,0</coordinates></Point></Placemark></Document></kml>"
        )
        state = self._preview(("Sites.kml", kml), ("notes.txt", b"The old mill on Route 9."))
        self.assertEqual(state["status"], "done", state)
        self.assertEqual(state["result"]["total"], 1)
        self.assertTrue(any(HERE in warning.lower() for warning in state["result"]["warnings"]), state["result"])


class TripSuggestionTests(_AiTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.trip = baker.make(Trip, name="Suggestion Test Trip", creator=self.profile)
        baker.make(
            TripMembership, trip=self.trip, profile=self.profile, status=TripMembership.STATUS_JOINED, rsvp="yes"
        )

    def test_nothing_is_cached_and_a_refresh_does_not_start_a_cooldown(self) -> None:
        from urbanlens.dashboard.services.trips import trip_ai_suggestions as module

        gateway = _Gateway('{"summary": "A plan."}')
        with deployment(DEVELOPMENT), mock.patch.object(module, "get_gateway", return_value=gateway):
            result = module.get_trip_suggestions(self.trip, self.profile, force_refresh=True)
        self.assertFalse(result.generated)
        self.assertIsNone(cache.get(module._cache_key(self.trip, self.profile)))
        self.assertIsNone(cache.get(module._cooldown_key(self.trip, self.profile)))
        self.assertNothingWasAsked(gateway)

    def test_it_works_the_moment_it_is_allowed(self) -> None:
        from urbanlens.dashboard.services.trips import trip_ai_suggestions as module

        gateway = _Gateway('{"summary": "A plan."}')
        with (
            deployment(DEVELOPMENT),
            mock.patch.object(module, "get_gateway", return_value=gateway),
            mock.patch.object(module, "build_trip_context"),
            mock.patch.object(module, "_format_prompt", return_value="p"),
        ):
            self.assertFalse(module.get_trip_suggestions(self.trip, self.profile, force_refresh=True).generated)
        with (
            deployment(DEVELOPMENT, overrides={"trip_suggestions": 1.0}),
            mock.patch.object(module, "get_gateway", return_value=gateway),
            mock.patch.object(module, "build_trip_context"),
            mock.patch.object(module, "_format_prompt", return_value="p"),
        ):
            result = module.get_trip_suggestions(self.trip, self.profile, force_refresh=True)
        self.assertTrue(result.generated)

    def test_the_panel_says_not_available_in_this_environment(self) -> None:
        self.client.force_login(self.user)
        with deployment(DEVELOPMENT):
            response = self.client.get(reverse("trips.ai_suggestions", args=[self.trip.slug]))
        self.assertEqual(response.status_code, 200)
        body = response.content.decode().lower()
        self.assertIn(HERE, body)
        self.assertNotIn("turned off", body)
        self.assertNotIn("ai-settings-section", body)

    def test_the_panel_still_says_turned_off_where_the_account_switched_it_off(self) -> None:
        Profile.objects.filter(pk=self.profile.pk).update(ai_enabled=False)
        self.client.force_login(self.user)
        response = self.client.get(reverse("trips.ai_suggestions", args=[self.trip.slug]))
        self.assertContains(response, "turned off")


class SuggestionTests(_AiTestCase):
    def test_a_label_style_suggestion_is_empty_without_a_call(self) -> None:
        from urbanlens.dashboard.services.labels import style_suggestions

        gateway = _Gateway(answers=["🏭", "#aabbcc"])
        with (
            deployment(DEVELOPMENT),
            mock.patch("urbanlens.dashboard.services.ai.factory.get_gateway", return_value=gateway),
        ):
            suggestion = style_suggestions.suggest_label_style("Factories", self.profile)
        self.assertEqual((suggestion.icon, suggestion.color), (None, None))
        self.assertNothingWasAsked(gateway)

    def test_a_category_suggestion_matches_nothing_without_a_call(self) -> None:
        from urbanlens.dashboard.services.labels.auto_tag import AutoTagService

        gateway = _Gateway(answers=["Factory"])
        with (
            deployment(DEVELOPMENT),
            mock.patch("urbanlens.dashboard.services.ai.factory.get_gateway", return_value=gateway),
            mock.patch.object(AutoTagService, "_build_prompt", return_value="p"),
            mock.patch.object(AutoTagService, "_build_instructions", return_value="i"),
        ):
            matched = AutoTagService()._ai_match(mock.Mock(), [mock.Mock(name="label")], "category")
        self.assertEqual(matched, [])
        self.assertNothingWasAsked(gateway)


class PhotoKeywordTests(_AiTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.image = baker.make("dashboard.Image", profile=self.profile, _create_files=True)

    def test_neither_hosted_provider_runs_for_an_upload_here(self) -> None:
        from urbanlens.dashboard.plugins.builtin.photo_keywords import (
            AiVisionKeywordProvider,
            ClassifierKeywordProvider,
        )

        with (
            mock.patch(
                "urbanlens.UrbanLens.settings.app.settings.cloudflare_worker_ai_endpoint", "https://example.test/ai"
            ),
            mock.patch("urbanlens.UrbanLens.settings.app.settings.cloudflare_ai_api_key", "key"),
        ):
            self.assertTrue(AiVisionKeywordProvider().is_available_for(self.image))
            self.assertTrue(ClassifierKeywordProvider().is_available_for(self.image))
            with deployment(DEVELOPMENT):
                self.assertFalse(AiVisionKeywordProvider().is_available_for(self.image))
                self.assertFalse(ClassifierKeywordProvider().is_available_for(self.image))
            with deployment(DEVELOPMENT, overrides={"ai_photo_keywords": 1.0}):
                self.assertTrue(AiVisionKeywordProvider().is_available_for(self.image))
                self.assertFalse(ClassifierKeywordProvider().is_available_for(self.image))

    def test_a_local_vision_model_is_still_offered(self) -> None:
        from urbanlens.dashboard.plugins.builtin.ollama import OllamaVisionKeywordProvider

        with (
            deployment(DEVELOPMENT),
            mock.patch("urbanlens.UrbanLens.settings.app.settings.ollama_base_url", "http://localhost:11434"),
        ):
            self.assertTrue(OllamaVisionKeywordProvider().is_available_for(self.image))

    def test_the_calls_themselves_make_no_request_and_say_nothing_was_learned(self) -> None:
        from urbanlens.dashboard.services.ai import vision

        client = mock.Mock()
        with (
            deployment(DEVELOPMENT),
            mock.patch("urbanlens.dashboard.services.ai.inference_client.get_inference_client", return_value=client),
        ):
            self.assertIsNone(vision.describe_photo_keywords(b"jpeg"))
            self.assertIsNone(vision.classify_photo(b"jpeg"))
        client.send.assert_not_called()
        client.classify.assert_not_called()
        self.assertFalse(ApiCallLog.objects.exists())

    def test_the_keywords_a_photo_has_survive_a_provider_that_was_not_opted_in(self) -> None:
        """Let start by Cloudflare's override, the site's provider is OpenAI: the transport refuses inside the slot."""
        from urbanlens.dashboard.plugins.builtin.photo_keywords import AiVisionKeywordProvider
        from urbanlens.dashboard.services.ai.inference_client import RemoteInferenceClient
        from urbanlens.dashboard.services.photos.photo_keywords import generate_keywords_for_image

        baker.make(ImageKeyword, image=self.image, source=AiVisionKeywordProvider.slug, keyword="brick")
        remote = RemoteInferenceClient("http://ai-inference", "token", timeout_seconds=5.0)
        with (
            deployment(DEVELOPMENT, overrides={"ai_cloudflare": 1.0}),
            mock.patch(
                "urbanlens.dashboard.plugins.registry.plugin_registry.photo_keyword_providers",
                return_value=[AiVisionKeywordProvider()],
            ),
            mock.patch(
                "urbanlens.dashboard.plugins.builtin.photo_keywords.require_analysis_jpeg_bytes", return_value=b"jpeg"
            ),
            mock.patch("urbanlens.dashboard.services.ai.vision._vision_target", return_value=("openai", "gpt")),
            mock.patch("urbanlens.dashboard.services.ai.inference_client.get_inference_client", return_value=remote),
            mock.patch("urbanlens.dashboard.services.ai.inference_client.requests.post") as post,
        ):
            counts = generate_keywords_for_image(self.image.pk)
        self.assertEqual(counts, {})
        self.assertTrue(ImageKeyword.objects.filter(image=self.image, keyword="brick").exists())
        post.assert_not_called()
        self.assertFalse(ApiCallLog.objects.exists(), "the refused call left a ledger row behind")


class OverridesLetTheWorkBeDoneTests(_AiTestCase):
    def test_an_opted_in_feature_makes_its_call_and_logs_it(self) -> None:
        from urbanlens.dashboard.services.trivia import answer_check

        gateway = _Gateway("MATCH")
        with (
            deployment(DEVELOPMENT, overrides={"trivia_answer_check": 1.0}),
            mock.patch.object(answer_check, "get_gateway", return_value=gateway),
        ):
            self.assertTrue(answer_check.is_answer_equivalent("nineteen thirty seven", "1937", profile=self.profile))
        self.assertEqual(gateway.calls, 1)
        self.assertEqual(ApiCallLog.objects.filter(service="trivia_answer_check").count(), 1)


class _TransportRefuses(_Gateway):
    """A gateway whose provider is refused at the inference client, after its feature's slot opened."""

    def _refuse(self):
        from urbanlens.dashboard.services.ai.inference_client import require_provider_egress

        self.calls += 1
        require_provider_egress("cloudflare")
        raise AssertionError("the provider was not refused: the deployment opts only OpenAI in")

    def send_prompt(self, prompt: str, **kwargs):
        self._refuse()

    def send_prompt_list(self, prompt: str, **kwargs):
        self._refuse()


class ARefusalTheSlotDidNotSeeTests(_AiTestCase):
    """Let start by an OpenAI override, the provider the site picked (Cloudflare) is refused inside the slot, and several callers
    wrap the call in ``except Exception``. It is still "not available here", not an error, and still no row."""

    def test_a_refusal_outside_any_slot_writes_no_row(self) -> None:
        from urbanlens.dashboard.services.ai.call_log import recorded_ai_call
        from urbanlens.dashboard.services.ai.inference_client import require_provider_egress

        with (
            deployment(DEVELOPMENT),
            self.assertRaises(EnvironmentRefusedError),
            recorded_ai_call(service="trivia_generation", provider="cloudflare", model="m"),
        ):
            require_provider_egress("cloudflare")
        self.assertFalse(ApiCallLog.objects.exists())

    def test_a_real_gateway_refused_by_the_client_writes_no_row(self) -> None:
        from urbanlens.dashboard.services.ai.cloudflare import CloudflareGateway
        from urbanlens.dashboard.services.ai.inference_client import RemoteInferenceClient

        gateway = CloudflareGateway(feature="trivia_generation")
        gateway._inference_client = RemoteInferenceClient("http://ai-inference", "token", timeout_seconds=5.0)
        with (
            deployment(DEVELOPMENT),
            mock.patch("urbanlens.dashboard.services.ai.inference_client.requests.post") as post,
            self.assertRaises(EnvironmentRefusedError),
        ):
            gateway._get_response(gateway.construct_messages("hello"))
        post.assert_not_called()
        self.assertFalse(ApiCallLog.objects.exists())

    def test_a_call_that_failed_for_another_reason_is_still_recorded_as_failed(self) -> None:
        from urbanlens.dashboard.services.ai.call_log import recorded_ai_call

        with (
            self.assertRaises(RuntimeError),
            recorded_ai_call(service="trivia_generation", provider="cloudflare", model="m"),
        ):
            raise RuntimeError("the provider fell over")
        self.assertEqual(list(ApiCallLog.objects.values_list("service", "success")), [("trivia_generation", False)])

    def test_link_extraction_says_so_rather_than_logging_a_failed_call(self) -> None:
        from urbanlens.dashboard.services.ai import link_extraction

        pin = baker.make(Pin, profile=self.profile, name="Old Mill", name_is_user_provided=True)
        extraction = LinkExtraction.objects.create(profile=self.profile, pin=pin, url="https://example.com/a")
        gateway = _TransportRefuses()
        with (
            deployment(DEVELOPMENT, overrides={"ai_openai": 1.0}),
            mock.patch.object(link_extraction, "fetch_page_text", return_value="page"),
            mock.patch("urbanlens.dashboard.services.ai.factory.get_gateway", return_value=gateway),
            self.assertNoLogs(link_extraction.logger, "ERROR"),
        ):
            link_extraction.run_extraction(extraction)
        extraction.refresh_from_db()
        self.assertEqual(extraction.status, LinkExtractionStatus.FAILED)
        self.assertIn(HERE, extraction.error.lower())
        self.assertFalse(ApiCallLog.objects.exists())

    def test_the_trivia_moderation_and_safety_review_pass_it_on_to_a_sweep(self) -> None:
        from urbanlens.dashboard.services.ai import article_safety
        from urbanlens.dashboard.services.trivia import classifier

        location = baker.make(Location)
        with (
            deployment(DEVELOPMENT, overrides={"ai_openai": 1.0}),
            mock.patch.object(classifier, "get_gateway", return_value=_TransportRefuses()),
            mock.patch("urbanlens.dashboard.services.ai.factory.get_gateway", return_value=_TransportRefuses()),
            self.assertNoLogs(classifier.logger, "ERROR"),
            self.assertNoLogs(article_safety.logger, "ERROR"),
        ):
            with self.assertRaises(EnvironmentRefusedError):
                classifier.classify_trivia_question("When?", "1937", location, raise_refusal=True)
            with self.assertRaises(EnvironmentRefusedError):
                article_safety.classify_article_text("The mill opened in 1937.", place_name="Mill", raise_refusal=True)
            # A direct caller still gets the fail-closed verdict, and nothing is reported as an unexpected failure.
            self.assertEqual(classifier.classify_trivia_question("When?", "1937", location).reason, "ai_unavailable")
            self.assertEqual(article_safety.classify_article_text("Text.", place_name="Mill").reason, "ai_unavailable")

    def test_the_wiki_draft_passes_it_on_to_the_sweep(self) -> None:
        from urbanlens.dashboard.services.trivia import wiki_incorporation

        with (
            deployment(DEVELOPMENT, overrides={"ai_openai": 1.0}),
            mock.patch.object(wiki_incorporation, "get_gateway", return_value=_TransportRefuses()),
            self.assertNoLogs(wiki_incorporation.logger, "ERROR"),
            self.assertRaises(EnvironmentRefusedError),
        ):
            wiki_incorporation._draft_paragraph(
                place_name="Mill", prompt="When?", answer="1937", existing_article="", raise_refusal=True
            )

    def test_an_answer_check_is_a_no_match_without_an_error(self) -> None:
        from urbanlens.dashboard.services.trivia import answer_check

        with (
            deployment(DEVELOPMENT, overrides={"ai_openai": 1.0}),
            mock.patch.object(answer_check, "get_gateway", return_value=_TransportRefuses()),
            self.assertNoLogs(answer_check.logger, "ERROR"),
        ):
            self.assertFalse(answer_check.is_answer_equivalent("nineteen thirty seven", "1937", profile=self.profile))
