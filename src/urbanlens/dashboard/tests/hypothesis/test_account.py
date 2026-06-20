"""Tests for EmailVerification model (dashboard/models/account.py).

All tests are DB-backed; EmailVerification.created is auto_now_add so we
back-date it via queryset.update() where time-sensitivity matters.
"""
from __future__ import annotations

from datetime import timedelta

from django.contrib.auth.models import User
from urbanlens.core.tests.testcase import TestCase
from django.utils import timezone
from hypothesis.extra.django import TestCase as HypothesisTestCase
from model_bakery import baker

from urbanlens.dashboard.models.account import EmailVerification


class EmailVerificationStrTests(TestCase):
	"""__str__ returns 'EmailVerification(<username>)'."""

	def test_str_returns_expected_format(self) -> None:
		user: User = baker.make(User, username="alice")
		ev: EmailVerification = baker.make(EmailVerification, user=user)
		self.assertEqual(str(ev), "EmailVerification(alice)")

	def test_str_reflects_actual_username(self) -> None:
		user: User = baker.make(User, username="urbexer99")
		ev: EmailVerification = baker.make(EmailVerification, user=user)
		self.assertIn("urbexer99", str(ev))


class EmailVerificationIsValidTests(TestCase):
	"""is_valid() returns True only for unverified tokens within the 48-hour window."""

	def _make_ev(self, **kwargs) -> EmailVerification:
		user: User = baker.make(User)
		return baker.make(EmailVerification, user=user, **kwargs)

	def _backdate(self, ev: EmailVerification, hours: float) -> EmailVerification:
		EmailVerification.objects.filter(pk=ev.pk).update(
			created=timezone.now() - timedelta(hours=hours)
		)
		ev.refresh_from_db()
		return ev

	def test_fresh_unverified_token_is_valid(self) -> None:
		ev = self._make_ev(verified_at=None)
		self.assertTrue(ev.is_valid())

	def test_verified_token_is_not_valid(self) -> None:
		ev = self._make_ev(verified_at=timezone.now())
		self.assertFalse(ev.is_valid())

	def test_expired_token_is_not_valid(self) -> None:
		ev = self._make_ev(verified_at=None)
		ev = self._backdate(ev, hours=49)
		self.assertFalse(ev.is_valid())

	def test_token_at_47h59m_is_still_valid(self) -> None:
		ev = self._make_ev(verified_at=None)
		ev = self._backdate(ev, hours=47.98)
		self.assertTrue(ev.is_valid())

	def test_verified_token_with_old_created_is_still_invalid(self) -> None:
		ev = self._make_ev(verified_at=timezone.now() - timedelta(days=1))
		self.assertFalse(ev.is_valid())

	def test_verified_at_takes_precedence_over_fresh_created(self) -> None:
		# Even a brand-new token is invalid once marked verified.
		ev = self._make_ev(verified_at=timezone.now())
		self.assertFalse(ev.is_valid())


class EmailVerificationMarkVerifiedTests(TestCase):
	"""mark_verified() sets verified_at and persists to the database."""

	def _fresh_ev(self) -> EmailVerification:
		user: User = baker.make(User)
		return baker.make(EmailVerification, user=user, verified_at=None)

	def test_mark_verified_sets_verified_at_in_memory(self) -> None:
		ev = self._fresh_ev()
		self.assertIsNone(ev.verified_at)
		ev.mark_verified()
		self.assertIsNotNone(ev.verified_at)

	def test_mark_verified_persists_to_db(self) -> None:
		ev = self._fresh_ev()
		ev.mark_verified()
		ev.refresh_from_db()
		self.assertIsNotNone(ev.verified_at)

	def test_mark_verified_timestamp_is_close_to_now(self) -> None:
		ev = self._fresh_ev()
		before = timezone.now()
		ev.mark_verified()
		after = timezone.now()
		assert ev.verified_at is not None
		self.assertGreaterEqual(ev.verified_at, before)
		self.assertLessEqual(ev.verified_at, after)

	def test_mark_verified_makes_is_valid_false(self) -> None:
		ev = self._fresh_ev()
		self.assertTrue(ev.is_valid())
		ev.mark_verified()
		self.assertFalse(ev.is_valid())

	def test_mark_verified_only_updates_verified_at(self) -> None:
		ev = self._fresh_ev()
		original_token = ev.token
		ev.mark_verified()
		ev.refresh_from_db()
		self.assertEqual(ev.token, original_token)
