"""Property-based tests for the Review model.

Covers:
- Rating field validation (MinValueValidator(0), MaxValueValidator(5))
- unique_together (user, pin) constraint
- Pin.rating property delegates to the latest review
"""
from __future__ import annotations

from hypothesis import HealthCheck, given, settings
from hypothesis.extra.django import TestCase as HypothesisTestCase
from model_bakery import baker

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction

from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.reviews.model import Review
from urbanlens.dashboard.tests.hypothesis.strategies import (
	invalid_rating_high,
	invalid_rating_low,
	valid_rating,
)

_DB_SETTINGS = dict(
	max_examples=40,
	deadline=None,
	suppress_health_check=[HealthCheck.too_slow, HealthCheck.filter_too_much],
)


class ReviewRatingBoundsTests(HypothesisTestCase):
	"""Rating must be in [0, 5]; values outside must fail validation."""

	def setUp(self) -> None:
		super().setUp()
		self.user = baker.make("auth.User")
		self.profile = baker.make("dashboard.Profile", user=self.user)
		self.pin = baker.make(Pin, profile=self.profile)

	@given(valid_rating)
	@settings(**_DB_SETTINGS)
	def test_valid_rating_passes_validation(self, rating: int) -> None:
		review = Review(rating=rating, review="test", user=self.user, pin=self.pin)
		try:
			review.full_clean()  # must not raise
		except ValidationError as exc:
			self.fail(f"full_clean() raised ValidationError for valid rating {rating}: {exc}")

	@given(invalid_rating_low)
	@settings(**_DB_SETTINGS)
	def test_rating_below_zero_fails_validation(self, rating: int) -> None:
		review = Review(rating=rating, review="test", user=self.user, pin=self.pin)
		with self.assertRaises(ValidationError, msg=f"Rating {rating} should fail validation"):
			review.full_clean()

	@given(invalid_rating_high)
	@settings(**_DB_SETTINGS)
	def test_rating_above_five_fails_validation(self, rating: int) -> None:
		review = Review(rating=rating, review="test", user=self.user, pin=self.pin)
		with self.assertRaises(ValidationError, msg=f"Rating {rating} should fail validation"):
			review.full_clean()

	def test_boundary_zero_is_valid(self) -> None:
		review = baker.make(Review, user=self.user, pin=self.pin, rating=0)
		review.full_clean()

	def test_boundary_five_is_valid(self) -> None:
		review = baker.make(Review, user=self.user, pin=self.pin, rating=5)
		review.full_clean()


class ReviewUniqueConstraintTests(HypothesisTestCase):
	"""Each (user, pin) pair must have at most one Review."""

	def setUp(self) -> None:
		super().setUp()
		self.user = baker.make("auth.User")
		self.profile = baker.make("dashboard.Profile", user=self.user)
		self.pin = baker.make(Pin, profile=self.profile)

	@given(valid_rating, valid_rating)
	@settings(**_DB_SETTINGS)
	def test_duplicate_user_pin_review_raises_integrity_error(
		self,
		rating1: int,
		rating2: int,
	) -> None:
		baker.make(Review, user=self.user, pin=self.pin, rating=rating1)
		with self.assertRaises((IntegrityError, ValidationError)):
			with transaction.atomic():
				baker.make(Review, user=self.user, pin=self.pin, rating=rating2)

	@given(valid_rating)
	@settings(**_DB_SETTINGS)
	def test_same_user_different_pins_are_independent(self, rating: int) -> None:
		other_pin = baker.make(Pin, profile=self.profile)
		baker.make(Review, user=self.user, pin=self.pin, rating=rating)
		try:
			baker.make(Review, user=self.user, pin=other_pin, rating=rating)
		except (IntegrityError, ValidationError) as exc:
			self.fail(f"Reviews for different pins should be independent: {exc}")

	@given(valid_rating)
	@settings(**_DB_SETTINGS)
	def test_different_users_same_pin_are_independent(self, rating: int) -> None:
		other_user = baker.make("auth.User")
		baker.make(Review, user=self.user, pin=self.pin, rating=rating)
		try:
			baker.make(Review, user=other_user, pin=self.pin, rating=rating)
		except (IntegrityError, ValidationError) as exc:
			self.fail(f"Reviews from different users for the same pin should be independent: {exc}")


class PinRatingPropertyTests(HypothesisTestCase):
	"""Pin.rating returns the latest review rating, or 0 if none exist."""

	def setUp(self) -> None:
		super().setUp()
		self.user = baker.make("auth.User")
		self.profile = baker.make("dashboard.Profile", user=self.user)

	def test_rating_is_zero_when_no_reviews(self) -> None:
		pin = baker.make(Pin, profile=self.profile)
		self.assertEqual(pin.rating, 0)

	@given(valid_rating)
	@settings(**_DB_SETTINGS)
	def test_rating_reflects_stored_review(self, rating: int) -> None:
		pin = baker.make(Pin, profile=self.profile)
		baker.make(Review, user=self.user, pin=pin, rating=rating)
		# Re-fetch to clear any cached state.
		pin.refresh_from_db()
		self.assertEqual(pin.rating, rating)

	@given(valid_rating, valid_rating)
	@settings(**_DB_SETTINGS)
	def test_rating_returns_latest_when_updated(self, first: int, second: int) -> None:
		"""Updating the review must change the reported pin rating."""
		pin = baker.make(Pin, profile=self.profile)
		review = baker.make(Review, user=self.user, pin=pin, rating=first)
		review.rating = second
		review.save()
		pin.refresh_from_db()
		self.assertEqual(pin.rating, second)
