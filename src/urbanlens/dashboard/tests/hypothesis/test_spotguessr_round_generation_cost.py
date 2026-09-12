"""Round generation must not re-run the eligibility query per retry."""

from __future__ import annotations

from unittest import mock

from django.contrib.auth.models import User
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.services.spotguessr import eligibility, session as spotguessr_session

_ELIGIBLE = "urbanlens.dashboard.services.spotguessr.eligibility.eligible_locations"


class RoundGenerationCostTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # absorbs the bootstrap site-admin promotion
        self.profile = baker.make(User).profile
        self.locations = [
            Location.objects.create(latitude=40.0 + n / 1000, longitude=-74.0 - n / 1000) for n in range(6)
        ]
        for location in self.locations:
            baker.make(Pin, profile=self.profile, location=location)
        self.config = spotguessr_session.GameConfig()

    def _pick(self, *, failures: int):
        """Run generation with a strategy that rejects the first ``failures`` locations."""
        calls = {"n": 0}

        def build_round(location, config, participants):
            calls["n"] += 1
            return None if calls["n"] <= failures else {"built": True}

        strategy = mock.Mock(build_round=mock.Mock(side_effect=build_round))
        with (
            mock.patch("urbanlens.dashboard.services.spotguessr.modes.get_strategy", return_value=strategy),
            mock.patch(_ELIGIBLE, wraps=eligibility.eligible_locations) as eligible,
        ):
            result = spotguessr_session.generate_round_content(
                mode="classic",
                config=self.config,
                participants=[self.profile],
                excluded_location_ids=[],
                previous_location=None,
            )
        return result, eligible.call_count

    def test_eligibility_is_computed_once_when_the_first_location_works(self) -> None:
        result, eligibility_calls = self._pick(failures=0)

        self.assertIsNotNone(result)
        self.assertEqual(eligibility_calls, 1)

    def test_eligibility_is_still_computed_once_across_several_retries(self) -> None:
        result, eligibility_calls = self._pick(failures=3)

        self.assertIsNotNone(result, "a later location should still be found")
        self.assertEqual(eligibility_calls, 1, "the expensive query must not repeat per attempt")

    def test_a_rejected_location_is_not_offered_again(self) -> None:
        seen: list[int] = []

        def build_round(location, config, participants):
            if location.pk in seen:
                raise AssertionError(f"location {location.pk} was offered twice")
            seen.append(location.pk)
            return None if len(seen) <= 2 else {"built": True}

        strategy = mock.Mock(build_round=mock.Mock(side_effect=build_round))
        with mock.patch("urbanlens.dashboard.services.spotguessr.modes.get_strategy", return_value=strategy):
            result = spotguessr_session.generate_round_content(
                mode="classic",
                config=self.config,
                participants=[self.profile],
                excluded_location_ids=[],
                previous_location=None,
            )

        self.assertIsNotNone(result)
        self.assertEqual(len(seen), 3)

    def test_an_up_front_exclusion_is_honoured(self) -> None:
        excluded = self.locations[0].pk
        strategy = mock.Mock(build_round=mock.Mock(return_value={"built": True}))

        with mock.patch("urbanlens.dashboard.services.spotguessr.modes.get_strategy", return_value=strategy):
            result = spotguessr_session.generate_round_content(
                mode="classic",
                config=self.config,
                participants=[self.profile],
                excluded_location_ids=[excluded],
                previous_location=None,
            )

        self.assertIsNotNone(result)
        self.assertNotEqual(result[0].pk, excluded)
