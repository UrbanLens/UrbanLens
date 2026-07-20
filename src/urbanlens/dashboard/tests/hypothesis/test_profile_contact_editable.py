"""Tests for the profile page's click-to-edit-in-place contact fields + birthday.

Covers:
- Own-profile view renders each of the 6 contact fields (phone/whatsapp/
  signal/telegram/discord/matrix) and birth_date as a click-to-edit element
  only once it actually has a value - an empty field (and, at the section
  level, an entirely-empty Contact/Additional Details section) is hidden
  rather than shown with an "Add ..." placeholder; adding a first value is
  the Edit Profile page's job, not this page's.
- Other viewers see plain read-only text (only for populated fields, same
  as before) and the section still disappears entirely when nothing to show.
- ProfileFieldUpdateView's field="phone_number"/etc and field="birth_date"
  POST paths, previously untested despite already existing.
"""

from __future__ import annotations

from django.contrib.auth.models import User
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.profile.meta import VisibilityChoice

_CONTACT_FIELDS = ("phone_number", "whatsapp_number", "signal_username", "telegram_username", "discord_username", "matrix_handle")
_CONTACT_EDITABLE_CLASSES = {
    "phone_number": "profile-phone-editable",
    "whatsapp_number": "profile-whatsapp-editable",
    "signal_username": "profile-signal-editable",
    "telegram_username": "profile-telegram-editable",
    "discord_username": "profile-discord-editable",
    "matrix_handle": "profile-matrix-editable",
}


class ProfileContactEditableRenderingTests(TestCase):
    """Own-profile view only: contact fields render as click-to-edit elements."""

    def setUp(self) -> None:
        self.user = baker.make(User)
        self.profile = self.user.profile
        self.client.force_login(self.user)

    def _get_own(self):
        return self.client.get(reverse("profile.view"))

    def test_contact_section_hidden_for_owner_with_no_contact_info_yet(self) -> None:
        """Same "hide the section entirely" behavior as other viewers now -
        an owner adds a first contact value via the Edit Profile page."""
        response = self._get_own()
        self.assertNotContains(response, ">Contact<")
        for cls in _CONTACT_EDITABLE_CLASSES.values():
            # The wiring script's `simpleTextField('.profile-xxx-editable', ...)`
            # calls legitimately contain each class name as inert text on
            # every render regardless of field population (see the hero-meta
            # test's identical caveat) - check the actual rendered element's
            # class list, not just "does this string appear anywhere in the
            # page source".
            self.assertNotContains(response, f'contact-info-item {cls}"')

    def test_populated_field_carries_the_raw_value(self) -> None:
        self.profile.phone_number = "555-0100"
        self.profile.save(update_fields=["phone_number"])
        response = self._get_own()
        self.assertContains(response, 'data-raw-phone="555-0100"')
        self.assertContains(response, "555-0100")

    def test_only_populated_fields_render_as_editable_others_stay_hidden(self) -> None:
        self.profile.phone_number = "555-0100"
        self.profile.save(update_fields=["phone_number"])
        response = self._get_own()
        self.assertContains(response, 'contact-info-item profile-phone-editable"')
        for field, cls in _CONTACT_EDITABLE_CLASSES.items():
            if field != "phone_number":
                self.assertNotContains(response, f'contact-info-item {cls}"')

    def test_other_viewer_sees_plain_text_for_populated_field_not_editable(self) -> None:
        self.profile.phone_number = "555-0100"
        self.profile.profile_visibility = VisibilityChoice.ANYONE
        self.profile.contact_visibility = VisibilityChoice.ANYONE
        self.profile.save(update_fields=["phone_number", "profile_visibility", "contact_visibility"])
        other = baker.make(User)
        self.client.force_login(other)

        response = self.client.get(reverse("profile.view_user", kwargs={"profile_slug": self.profile.slug or self.profile.ensure_slug()}))
        self.assertContains(response, "555-0100")
        # Same "inert wiring-script text" caveat as the hero-meta/bio
        # precedents - check the actual rendered element's class, not just
        # string presence anywhere in the page (the JS selector strings
        # legitimately appear as inert text regardless of viewer).
        self.assertNotContains(response, 'profile-phone-editable"')

    def test_other_viewer_sees_no_contact_section_when_all_empty(self) -> None:
        self.profile.profile_visibility = VisibilityChoice.ANYONE
        self.profile.contact_visibility = VisibilityChoice.ANYONE
        self.profile.save(update_fields=["profile_visibility", "contact_visibility"])
        other = baker.make(User)
        self.client.force_login(other)

        response = self.client.get(reverse("profile.view_user", kwargs={"profile_slug": self.profile.slug or self.profile.ensure_slug()}))
        self.assertNotContains(response, ">Contact<")


class ProfileBirthDateEditableRenderingTests(TestCase):
    """Own-profile view only: birth_date renders as a click-to-edit element."""

    def setUp(self) -> None:
        self.user = baker.make(User)
        self.profile = self.user.profile
        self.client.force_login(self.user)

    def _get_own(self):
        return self.client.get(reverse("profile.view"))

    def test_additional_details_hidden_for_owner_with_no_birth_date_and_no_secondary_emails(self) -> None:
        """Same reasoning as the Contact section - an owner adds a first
        birthday via the Edit Profile page rather than an inline placeholder."""
        response = self._get_own()
        self.assertNotContains(response, "Additional Details")
        # data-raw-birth-date only ever appears on the real editable span,
        # which is now only rendered once profile.birth_date has a value -
        # unlike the bare class name, the wiring script's
        # `wireMetaField({selector: '.profile-birth-date-editable', ...})`
        # call never emits this attribute name as inert text.
        self.assertNotContains(response, "data-raw-birth-date")

    def test_populated_birth_date_carries_the_raw_iso_value(self) -> None:
        self.profile.birth_date = "1990-06-15"
        self.profile.save(update_fields=["birth_date"])
        response = self._get_own()
        self.assertContains(response, 'data-raw-birth-date="1990-06-15"')
        self.assertContains(response, "Birthday: June 15, 1990")

    def test_additional_details_shown_for_secondary_email_alone_but_no_birth_date_row(self) -> None:
        """The section has two independent triggers - a secondary email with
        no birthday set yet must still show the section (for the email), just
        without the birth-date editable row itself."""
        from urbanlens.dashboard.models.profile.email import ProfileEmail

        baker.make(ProfileEmail, profile=self.profile, email="alt@example.com")
        response = self._get_own()
        self.assertContains(response, "Additional Details")
        self.assertContains(response, "alt@example.com")
        self.assertNotContains(response, "data-raw-birth-date")


class ProfileFieldUpdateContactAndBirthDateTests(TestCase):
    """ProfileFieldUpdateView's contact/birth_date field autosave paths."""

    def setUp(self) -> None:
        self.user = baker.make(User)
        self.profile = self.user.profile
        self.client.force_login(self.user)

    def _post(self, field: str, value: str):
        return self.client.post(reverse("profile.field.update"), {"field": field, "value": value})

    def test_updates_each_contact_field(self) -> None:
        for field in _CONTACT_FIELDS:
            response = self._post(field, "new-value")
            self.assertEqual(response.status_code, 200, field)
            self.profile.refresh_from_db()
            self.assertEqual(getattr(self.profile, field), "new-value", field)

    def test_clearing_a_contact_field_sets_it_to_empty(self) -> None:
        """phone_number etc. are CharField(blank=True, default="") - the empty
        sentinel is "", not None, unlike bio/area's null=True TextFields."""
        self.profile.phone_number = "555-0100"
        self.profile.save(update_fields=["phone_number"])
        response = self._post("phone_number", "")
        self.assertEqual(response.status_code, 200)
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.phone_number, "")

    def test_updates_birth_date(self) -> None:
        response = self._post("birth_date", "1990-06-15")
        self.assertEqual(response.status_code, 200)
        self.profile.refresh_from_db()
        self.assertEqual(str(self.profile.birth_date), "1990-06-15")

    def test_future_birth_date_is_rejected(self) -> None:
        response = self._post("birth_date", "2999-01-01")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"future", response.content)
        self.profile.refresh_from_db()
        self.assertIsNone(self.profile.birth_date)
