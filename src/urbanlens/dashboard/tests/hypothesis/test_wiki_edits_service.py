"""Property-based tests for ``services.wiki.wiki_edits.apply_wiki_edit``.

The strict/non-strict split is the whole point of this service, so the
properties asserted here are about that split rather than about any one input:

- strict mode never accepts a value it would have skipped;
- non-strict mode never *raises* on one (preserving the internal view's
  long-standing behavior, bug and all - see ``docs/PROBLEMS.md``);
- both modes agree exactly whenever every submitted value is valid.
"""

from __future__ import annotations

from datetime import date

from django.contrib.auth.models import User
from model_bakery import baker

from hypothesis import HealthCheck, given, settings, strategies as st
from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.abstract.choices import SecurityLevel
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.models.wiki_edit import WikiEdit
from urbanlens.dashboard.services.wiki.wiki_edits import (
    WIKI_SECURITY_FIELDS,
    WikiEditValidationError,
    apply_wiki_edit,
)

VALID_SECURITY = [choice.value for choice in SecurityLevel]

#: Strings that are never a valid SecurityLevel.
invalid_security = st.text(min_size=1, max_size=20).filter(lambda value: value not in VALID_SECURITY)

#: Strings that datetime.strptime(..., "%Y-%m-%d") will not parse.
invalid_dates = st.text(min_size=1, max_size=20).filter(lambda value: not _parses_as_date(value))


def _parses_as_date(value: str) -> bool:
    """Whether *value* parses as ``YYYY-MM-DD``."""
    from datetime import datetime

    try:
        datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        return False
    return True


class ApplyWikiEditPropertyTests(TestCase):
    """Strict and non-strict modes must differ only in how they reject."""

    def setUp(self) -> None:
        baker.make(User)  # first user auto-promoted to bootstrap site admin
        self.profile = Profile.objects.get(user=baker.make(User))

    def _fresh_wiki(self):
        """A wiki with no prior edits, for one property example."""
        location = baker.make("dashboard.Location")
        return baker.make("dashboard.Wiki", location=location, name="Baseline")

    @settings(max_examples=10, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
    @given(field=st.sampled_from(WIKI_SECURITY_FIELDS), value=invalid_security)
    def test_strict_mode_rejects_every_invalid_security_value(self, field: str, value: str) -> None:
        """An unrecognized level is always an error, never a silent skip."""
        wiki = self._fresh_wiki()
        with self.assertRaises(WikiEditValidationError):
            apply_wiki_edit(wiki, self.profile, {field: value}, strict=True)

        wiki.refresh_from_db()
        self.assertNotEqual(getattr(wiki, field), value)

    @settings(max_examples=10, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
    @given(field=st.sampled_from(WIKI_SECURITY_FIELDS), value=invalid_security)
    def test_non_strict_mode_never_raises_and_never_writes(self, field: str, value: str) -> None:
        """The internal path keeps skipping - quietly, but without corrupting data."""
        wiki = self._fresh_wiki()
        edit = apply_wiki_edit(wiki, self.profile, {field: value}, strict=False)

        self.assertIsNone(edit)
        wiki.refresh_from_db()
        self.assertNotEqual(getattr(wiki, field), value)

    @settings(max_examples=10, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
    @given(value=invalid_dates)
    def test_strict_mode_rejects_unparseable_dates(self, value: str) -> None:
        wiki = self._fresh_wiki()
        with self.assertRaises(WikiEditValidationError):
            apply_wiki_edit(wiki, self.profile, {"date_abandoned": value}, strict=True)

    @settings(max_examples=10, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
    @given(value=invalid_dates)
    def test_non_strict_mode_skips_unparseable_dates(self, value: str) -> None:
        wiki = self._fresh_wiki()
        self.assertIsNone(apply_wiki_edit(wiki, self.profile, {"date_abandoned": value}, strict=False))

    @settings(max_examples=10, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
    @given(
        field=st.sampled_from(WIKI_SECURITY_FIELDS),
        value=st.sampled_from(VALID_SECURITY),
    )
    def test_valid_values_are_applied_identically_in_both_modes(self, field: str, value: str) -> None:
        """When everything is valid, strict changes nothing about the outcome."""
        strict_wiki = self._fresh_wiki()
        lenient_wiki = self._fresh_wiki()

        strict_edit = apply_wiki_edit(strict_wiki, self.profile, {field: value}, strict=True)
        lenient_edit = apply_wiki_edit(lenient_wiki, self.profile, {field: value}, strict=False)

        strict_wiki.refresh_from_db()
        lenient_wiki.refresh_from_db()
        self.assertEqual(getattr(strict_wiki, field), getattr(lenient_wiki, field))

        # A no-op (value already equal to the default) legitimately records
        # nothing in either mode - what matters is that they agree.
        self.assertEqual(strict_edit is None, lenient_edit is None)


class ApplyWikiEditBehaviorTests(TestCase):
    """Concrete cases the property tests don't pin down."""

    def setUp(self) -> None:
        baker.make(User)
        self.profile = Profile.objects.get(user=baker.make(User))
        location = baker.make("dashboard.Location")
        self.wiki = baker.make("dashboard.Wiki", location=location, name="Baseline")

    def test_no_recognized_change_records_nothing(self) -> None:
        self.assertIsNone(apply_wiki_edit(self.wiki, self.profile, {"unrelated": "value"}, strict=True))
        self.assertFalse(WikiEdit.objects.filter(wiki=self.wiki).exists())

    def test_setting_the_same_value_is_not_an_edit(self) -> None:
        self.assertIsNone(apply_wiki_edit(self.wiki, self.profile, {"name": "Baseline"}, strict=True))

    def test_audit_row_records_from_and_to(self) -> None:
        edit = apply_wiki_edit(self.wiki, self.profile, {"name": "Renamed"}, strict=True)
        self.assertIsNotNone(edit)
        self.assertEqual(edit.changes["name"], {"from": "Baseline", "to": "Renamed"})

    def test_date_objects_are_accepted_directly(self) -> None:
        """DRF hands the service a real date; it must not require a string."""
        edit = apply_wiki_edit(self.wiki, self.profile, {"date_abandoned": date(1999, 6, 15)}, strict=True)
        self.assertIsNotNone(edit)
        self.assertEqual(self.wiki.date_abandoned, date(1999, 6, 15))

    def test_overlong_description_is_rejected_in_both_modes(self) -> None:
        """A too-long description was always a hard error, not a silent skip."""
        from urbanlens.dashboard.services.core.text_limits import MAX_WIKI_DESCRIPTION_LENGTH

        too_long = "x" * (MAX_WIKI_DESCRIPTION_LENGTH + 1)
        for strict in (True, False):
            with self.subTest(strict=strict), self.assertRaises(WikiEditValidationError):
                apply_wiki_edit(self.wiki, self.profile, {"description": too_long}, strict=strict)
