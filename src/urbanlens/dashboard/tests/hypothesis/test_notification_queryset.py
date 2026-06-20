"""Tests for NotificationLog QuerySet and Manager.

Covers:
- unread()       - lines 12-13
- for_profile()  - line 17
- mark_read()    - lines 21-22
"""
from __future__ import annotations

from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.notifications.meta import Status
from urbanlens.dashboard.models.notifications.model import NotificationLog


class NotificationQuerySetUnreadTests(TestCase):
	"""unread() returns only notifications with Status.UNREAD."""

	def setUp(self):
		self.user = baker.make("auth.User")
		self.profile = self.user.profile
		self.unread = baker.make(
			NotificationLog,
			profile=self.profile,
			status=Status.UNREAD,
			title="Unread one",
		)
		self.read = baker.make(
			NotificationLog,
			profile=self.profile,
			status=Status.READ,
			title="Already read",
		)
		self.dismissed = baker.make(
			NotificationLog,
			profile=self.profile,
			status=Status.DISMISSED,
			title="Dismissed",
		)

	def test_unread_includes_unread_notifications(self):
		qs = NotificationLog.objects.unread()
		self.assertIn(self.unread, qs)

	def test_unread_excludes_read_notifications(self):
		qs = NotificationLog.objects.unread()
		self.assertNotIn(self.read, qs)

	def test_unread_excludes_dismissed_notifications(self):
		qs = NotificationLog.objects.unread()
		self.assertNotIn(self.dismissed, qs)

	def test_unread_returns_nonempty_queryset(self):
		qs = NotificationLog.objects.unread()
		self.assertGreaterEqual(qs.count(), 1)


class NotificationQuerySetForProfileTests(TestCase):
	"""for_profile() scopes notifications to a single profile."""

	def setUp(self):
		self.u1 = baker.make("auth.User")
		self.u2 = baker.make("auth.User")
		self.n1 = baker.make(NotificationLog, profile=self.u1.profile, title="For u1")
		self.n2 = baker.make(NotificationLog, profile=self.u2.profile, title="For u2")

	def test_for_profile_includes_own_notifications(self):
		qs = NotificationLog.objects.for_profile(self.u1.profile)
		self.assertIn(self.n1, qs)

	def test_for_profile_excludes_other_profiles_notifications(self):
		qs = NotificationLog.objects.for_profile(self.u1.profile)
		self.assertNotIn(self.n2, qs)

	def test_for_profile_returns_all_matching(self):
		extra = baker.make(NotificationLog, profile=self.u1.profile, title="Also for u1")
		qs = NotificationLog.objects.for_profile(self.u1.profile)
		self.assertIn(self.n1, qs)
		self.assertIn(extra, qs)
		self.assertEqual(qs.count(), 2)


class NotificationQuerySetMarkReadTests(TestCase):
	"""mark_read() updates the status to READ and returns the count updated."""

	def setUp(self):
		self.user = baker.make("auth.User")
		self.profile = self.user.profile
		self.n1 = baker.make(
			NotificationLog, profile=self.profile, status=Status.UNREAD, title="A"
		)
		self.n2 = baker.make(
			NotificationLog, profile=self.profile, status=Status.UNREAD, title="B"
		)
		# One already-read; should not be double-counted.
		self.n3 = baker.make(
			NotificationLog, profile=self.profile, status=Status.READ, title="C"
		)

	def test_mark_read_returns_updated_count(self):
		count = NotificationLog.objects.filter(profile=self.profile).unread().mark_read()
		self.assertEqual(count, 2)

	def test_mark_read_sets_status_to_read(self):
		NotificationLog.objects.filter(profile=self.profile).unread().mark_read()
		self.n1.refresh_from_db()
		self.n2.refresh_from_db()
		self.assertEqual(self.n1.status, Status.READ)
		self.assertEqual(self.n2.status, Status.READ)

	def test_mark_read_does_not_change_already_read(self):
		NotificationLog.objects.filter(profile=self.profile).unread().mark_read()
		self.n3.refresh_from_db()
		self.assertEqual(self.n3.status, Status.READ)

	def test_mark_read_on_empty_queryset_returns_zero(self):
		other_user = baker.make("auth.User")
		count = NotificationLog.objects.for_profile(other_user.profile).mark_read()
		self.assertEqual(count, 0)

	def test_chained_unread_and_mark_read_clears_unread(self):
		# After marking read, unread() should return no notifications for this profile.
		NotificationLog.objects.for_profile(self.profile).unread().mark_read()
		remaining = NotificationLog.objects.for_profile(self.profile).unread()
		self.assertEqual(remaining.count(), 0)
