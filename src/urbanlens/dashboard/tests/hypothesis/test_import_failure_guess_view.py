"""The location guess is fetched per card, not built while rendering the queue.

A single import can leave hundreds of failures. Computing a guess for each one
while rendering the queue would make the page wait on hundreds of sequential
geocoder calls, and would spend that quota on cards the user never scrolls to -
so the card carries an ``hx-trigger="revealed once"`` placeholder and the guess
is fetched only when it comes into view.

The endpoint answers with an empty body when there is no confident guess, which
is the normal outcome for a vague name: the card then shows nothing extra rather
than an empty suggestion box.
"""

from __future__ import annotations

from unittest import mock

from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.pin_import_failures.model import PinImportFailure, PinImportFailureReason, PinImportFailureStatus
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.services.pins.import_failure_guess import LocationGuess

_GUESS = "urbanlens.dashboard.services.pins.import_failure_guess.guess_for_failure"


class ImportFailureGuessViewTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.user = baker.make("auth.User")
        self.profile = Profile.objects.get(user=self.user)
        self.client.force_login(self.user)
        self.failure = PinImportFailure.objects.create(
            profile=self.profile,
            cid=12345,
            name="Fort Wetherill",
            reason=PinImportFailureReason.LOOKUP_STALLED,
        )

    def _url(self, failure=None) -> str:
        return reverse("memories.locations.import_failures.guess", kwargs={"failure_id": (failure or self.failure).pk})

    def _a_guess(self) -> LocationGuess:
        return LocationGuess(latitude=41.47, longitude=-71.35, display_name="Fort Wetherill, Jamestown", source="name", confidence=0.6)

    def test_a_guess_is_rendered_with_its_coordinates(self) -> None:
        with mock.patch(_GUESS, return_value=self._a_guess()):
            response = self.client.get(self._url())

        self.assertEqual(response.status_code, 200)
        body = response.content.decode()
        self.assertIn("Fort Wetherill, Jamestown", body)
        self.assertIn("41.47", body)

    def test_no_guess_renders_nothing_at_all(self) -> None:
        """An empty suggestion box would be worse than no box."""
        with mock.patch(_GUESS, return_value=None):
            response = self.client.get(self._url())

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content.decode().strip(), "")

    def test_the_queue_does_not_compute_guesses_while_rendering(self) -> None:
        """The whole point of the lazy fetch: hundreds of cards, no geocoding."""
        for index in range(5):
            PinImportFailure.objects.create(
                profile=self.profile, cid=1000 + index, name=f"Place {index}", reason=PinImportFailureReason.LOOKUP_STALLED,
            )

        with mock.patch(_GUESS) as guess:
            response = self.client.get(reverse("memories.locations.import_failures.queue"))

        self.assertEqual(response.status_code, 200)
        guess.assert_not_called()

    def test_the_card_asks_for_its_guess_on_reveal(self) -> None:
        response = self.client.get(reverse("memories.locations.import_failures.queue"))

        body = response.content.decode()
        self.assertIn(self._url(), body)
        self.assertIn("revealed once", body)

    def test_another_profiles_failure_is_not_guessable(self) -> None:
        other = PinImportFailure.objects.create(
            profile=Profile.objects.get(user=baker.make("auth.User")),
            cid=999,
            name="Not mine",
            reason=PinImportFailureReason.LOOKUP_STALLED,
        )

        with mock.patch(_GUESS, return_value=self._a_guess()):
            response = self.client.get(self._url(other))

        self.assertEqual(response.status_code, 404)

    def test_a_resolved_failure_offers_no_guess(self) -> None:
        PinImportFailure.objects.filter(pk=self.failure.pk).update(status=PinImportFailureStatus.RESOLVED)

        with mock.patch(_GUESS) as guess:
            response = self.client.get(self._url())

        self.assertEqual(response.content.decode().strip(), "")
        guess.assert_not_called()

    def test_the_suggestion_only_prefills_and_never_places(self) -> None:
        """A guess from a name can be wrong, so the user confirms it."""
        with mock.patch(_GUESS, return_value=self._a_guess()):
            body = self.client.get(self._url()).content.decode()

        self.assertNotIn("hx-post", body)
        self.assertIn("showModal", body)
