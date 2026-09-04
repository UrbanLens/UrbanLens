"""Automatic tagging of pins is sourced from REData, not keyword matching.

Keyword tagging asked each user to write the phrases that should imply each of
their labels - work almost nobody did, so the feature mostly did nothing. The
pin path now asks REData which of the owner's own tag/category labels apply to
the place, and applies the confident ones.

What the tests pin down is the boundary, because the suggestion comes from
outside: REData answers about the profile's whole taxonomy, so eligibility
(the per-label opt-out, protected labels) is enforced *here* rather than
trusted upstream, and a low-confidence guess is not applied at all.

Wikis are deliberately unchanged: they have no owner, so there is no per-user
taxonomy for REData to match against.
"""

from __future__ import annotations

from unittest import mock

from django.contrib.auth.models import User
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.labels.meta import KIND_TAG
from urbanlens.dashboard.models.labels.model import Label
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.services.labels.auto_tag import REDATA_CONFIDENCE_FLOOR, AutoTagService

_SERVICE = "urbanlens.dashboard.services.labels.redata_suggestions.get_suggestions"


class RedataSourcedAutoTagTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # absorbs the bootstrap site-admin promotion
        self.user = baker.make(User)
        self.profile = self.user.profile
        self.pin = baker.make(Pin, profile=self.profile, parent_pin=None, name="Old power station")
        self.tag = baker.make(Label, profile=self.profile, kind=KIND_TAG, name="Power plant", allow_auto_tag=True)

    def _suggest(self, suggestions, *, has_feature: bool = True, apply: bool = False):
        with (
            mock.patch(_SERVICE, return_value=suggestions),
            mock.patch("urbanlens.dashboard.models.subscriptions.model.user_has_feature", return_value=has_feature),
        ):
            return AutoTagService(kinds=[KIND_TAG]).suggest_for_pin(self.pin, apply=apply)

    def test_a_confident_suggestion_is_applied(self) -> None:
        matched = self._suggest([(self.tag, 0.9)], apply=True)

        self.assertEqual([label.pk for label in matched], [self.tag.pk])
        self.assertIn(self.tag.pk, [label.pk for label in self.pin.labels.all()])

    def test_a_low_confidence_guess_is_not(self) -> None:
        """Applying a label is cheap to undo but annoying to find."""
        matched = self._suggest([(self.tag, REDATA_CONFIDENCE_FLOOR - 0.01)], apply=True)

        self.assertEqual(matched, [])
        self.assertEqual(list(self.pin.labels.all()), [])

    def test_a_label_the_user_excluded_is_never_applied(self) -> None:
        """Eligibility is enforced here, not trusted from the upstream answer."""
        self.tag.allow_auto_tag = False
        self.tag.save(update_fields=["allow_auto_tag"])

        matched = self._suggest([(self.tag, 0.99)], apply=True)

        self.assertEqual(matched, [])

        # Same label, opted back in - an "always excluded" implementation would still pass above.
        self.tag.allow_auto_tag = True
        self.tag.save(update_fields=["allow_auto_tag"])

        matched = self._suggest([(self.tag, 0.99)], apply=True)

        self.assertEqual([label.pk for label in matched], [self.tag.pk])

    def test_a_protected_label_is_never_applied(self) -> None:
        protected = baker.make(Label, profile=self.profile, kind=KIND_TAG, name="Protected", is_protected=True)

        matched = self._suggest([(protected, 0.99)], apply=True)

        self.assertEqual(matched, [])

        # Same label, unprotected - an "always protected" implementation would still pass above.
        protected.is_protected = False
        protected.save(update_fields=["is_protected"])

        matched = self._suggest([(protected, 0.99)], apply=True)

        self.assertEqual([label.pk for label in matched], [protected.pk])

    def test_nothing_happens_without_the_capability(self) -> None:
        matched = self._suggest([(self.tag, 0.99)], has_feature=False, apply=True)

        self.assertEqual(matched, [])

        # Same pin/tag, capability granted - an "always gated off" implementation would still pass above.
        matched = self._suggest([(self.tag, 0.99)], has_feature=True, apply=True)

        self.assertEqual([label.pk for label in matched], [self.tag.pk])

    def test_the_user_switch_turns_it_off(self) -> None:
        self.profile.disable_auto_tagging = True
        self.profile.save(update_fields=["disable_auto_tagging"])

        matched = self._suggest([(self.tag, 0.99)], apply=True)

        self.assertEqual(matched, [])

        # Same profile/tag, switch back on - an "always off" implementation would still pass above.
        self.profile.disable_auto_tagging = False
        self.profile.save(update_fields=["disable_auto_tagging"])

        matched = self._suggest([(self.tag, 0.99)], apply=True)

        self.assertEqual([label.pk for label in matched], [self.tag.pk])

    def test_redata_being_unavailable_is_quiet(self) -> None:
        """get_suggestions returns None when REData is unconfigured or fails."""
        matched = self._suggest(None, apply=True)

        self.assertEqual(matched, [])
        self.assertEqual(list(self.pin.labels.all()), [])

    def test_the_keyword_path_is_gone_from_the_pin_flow(self) -> None:
        """A label whose keywords match the pin name is not applied on that basis."""
        keyworded = baker.make(
            Label, profile=self.profile, kind=KIND_TAG, name="Station", keywords="power station", allow_auto_tag=True
        )

        matched = self._suggest([], apply=True)

        self.assertNotIn(keyworded.pk, [label.pk for label in matched])
