"""Property-based tests for ``services.wiki.wiki_edits.apply_wiki_edit``.

Two properties, asserted over generated values rather than chosen ones:

- a value the function will not store is rejected, never dropped. Both callers
  used to differ here - the internal view skipped the field and answered
  ``{"ok": true}``, reporting a write it had not made;
- a value equal to what the submitter was looking at is not an edit. These
  forms post every field whether or not it was touched, so the diff is the only
  thing between an untouched field and a `WikiEdit` with someone's name on it.
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
    """Whatever is submitted, an unstorable value is refused rather than dropped."""

    def setUp(self) -> None:
        baker.make(User)  # first user auto-promoted to bootstrap site admin
        self.profile = Profile.objects.get(user=baker.make(User))

    def _fresh_wiki(self):
        """A wiki with no prior edits, for one property example."""
        location = baker.make("dashboard.Location")
        return baker.make("dashboard.Wiki", location=location, name="Baseline")

    @settings(max_examples=10, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
    @given(field=st.sampled_from(WIKI_SECURITY_FIELDS), value=invalid_security)
    def test_an_invalid_security_value_is_rejected(self, field: str, value: str) -> None:
        """An unrecognized level is always an error, never a silent skip."""
        wiki = self._fresh_wiki()
        with self.assertRaises(WikiEditValidationError):
            apply_wiki_edit(wiki, self.profile, {field: value})

        wiki.refresh_from_db()
        self.assertNotEqual(getattr(wiki, field), value)

    @settings(max_examples=10, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
    @given(value=invalid_dates)
    def test_an_unparseable_date_is_rejected(self, value: str) -> None:
        wiki = self._fresh_wiki()
        with self.assertRaises(WikiEditValidationError):
            apply_wiki_edit(wiki, self.profile, {"date_abandoned": value})

    @settings(max_examples=10, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
    @given(
        field=st.sampled_from(WIKI_SECURITY_FIELDS),
        value=st.sampled_from(VALID_SECURITY),
    )
    def test_a_valid_value_is_applied(self, field: str, value: str) -> None:
        wiki = self._fresh_wiki()

        apply_wiki_edit(wiki, self.profile, {field: value})

        wiki.refresh_from_db()
        self.assertEqual(getattr(wiki, field), value)


class ApplyWikiEditBehaviorTests(TestCase):
    """Concrete cases the property tests don't pin down."""

    def setUp(self) -> None:
        baker.make(User)
        self.profile = Profile.objects.get(user=baker.make(User))
        location = baker.make("dashboard.Location")
        self.wiki = baker.make("dashboard.Wiki", location=location, name="Baseline")

    def test_an_untouched_empty_date_is_not_a_change(self) -> None:
        """The form posts every field, so a nullable one arrives as "".

        Against a stored `None` that differs as a string and normalises back to
        `None`, so it used to be recorded as a change - a `WikiEdit` saying
        None -> None, a bumped `updated`, and reputation paid for it.
        """
        self.assertIsNone(self.wiki.date_abandoned)

        self.assertIsNone(apply_wiki_edit(self.wiki, self.profile, {"date_abandoned": ""}))

        self.assertFalse(WikiEdit.objects.filter(wiki=self.wiki).exists())

    def test_an_untouched_empty_text_field_is_not_a_change(self) -> None:
        self.wiki.description = None
        self.wiki.save(update_fields=["description"])

        self.assertIsNone(apply_wiki_edit(self.wiki, self.profile, {"description": ""}))

        self.assertFalse(WikiEdit.objects.filter(wiki=self.wiki).exists())

    def test_clearing_a_date_that_was_set_is_still_a_change(self) -> None:
        """The guard must not swallow a real clear."""
        self.wiki.date_abandoned = date(1974, 3, 1)
        self.wiki.save(update_fields=["date_abandoned"])

        edit = apply_wiki_edit(self.wiki, self.profile, {"date_abandoned": ""})

        assert edit is not None
        self.assertEqual(list(edit.changes), ["date_abandoned"])
        self.wiki.refresh_from_db()
        self.assertIsNone(self.wiki.date_abandoned)

    def test_no_recognized_change_records_nothing(self) -> None:
        self.assertIsNone(apply_wiki_edit(self.wiki, self.profile, {"unrelated": "value"}))
        self.assertFalse(WikiEdit.objects.filter(wiki=self.wiki).exists())

    def test_setting_the_same_value_is_not_an_edit(self) -> None:
        self.assertIsNone(apply_wiki_edit(self.wiki, self.profile, {"name": "Baseline"}))

    def test_audit_row_records_from_and_to(self) -> None:
        edit = apply_wiki_edit(self.wiki, self.profile, {"name": "Renamed"})
        self.assertIsNotNone(edit)
        self.assertEqual(edit.changes["name"], {"from": "Baseline", "to": "Renamed"})

    def test_date_objects_are_accepted_directly(self) -> None:
        """DRF hands the service a real date; it must not require a string."""
        edit = apply_wiki_edit(self.wiki, self.profile, {"date_abandoned": date(1999, 6, 15)})
        self.assertIsNotNone(edit)
        self.assertEqual(self.wiki.date_abandoned, date(1999, 6, 15))

    def test_an_overlong_description_is_rejected(self) -> None:
        """The one rejection that was always hard, on both callers."""
        from urbanlens.dashboard.services.core.text_limits import MAX_WIKI_DESCRIPTION_LENGTH

        too_long = "x" * (MAX_WIKI_DESCRIPTION_LENGTH + 1)
        with self.assertRaises(WikiEditValidationError):
            apply_wiki_edit(self.wiki, self.profile, {"description": too_long})
