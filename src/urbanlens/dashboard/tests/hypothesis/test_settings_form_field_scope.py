"""A settings form must write only the profile fields it owns.

The settings page splits a profile's preferences across fifteen small
``ModelForm``s, each posted independently. Every one of them ends in Django's
default ``ModelForm.save()``, which calls ``instance.save()`` with no
``update_fields`` - a whole-row write of every column from the instance the
request loaded.

``Profile`` is the most contested row in the app. Around twenty-five other
writers scope their updates to the columns they own, among them:

- the pin-create signal, which clears the cached map centre so it is recomputed;
- the importer, which writes the privacy and contact blocks by targeted update
  over a job that runs for minutes;
- the home-widget layout, the map-suggestions intro flag, and the external API's
  own settings patch (``profile.save(update_fields=[*touched, "updated"])``).

A whole-row save from a form reverts whatever any of them committed while the
settings page was open.

The forms module already contains the realisation, applied once and locally:
``MapCenterForm.save`` re-reads ``map_custom_latitude``/``longitude`` from the
database because the hidden fields "must not overwrite the user's saved custom
location". That is this bug, noticed for one pair of columns on one form, and
fixed by hand there rather than at the shape that causes it.
"""

from __future__ import annotations

from django.contrib.auth.models import User
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.forms.settings_form import HistorySettingsForm, ProfileSettingsForm
from urbanlens.dashboard.models.profile.model import Profile


class SettingsFormFieldScopeTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # first user is auto-promoted to site admin
        self.profile = baker.make(User).profile

    def _request_snapshot(self) -> Profile:
        """The instance a settings POST binds its form to."""
        return Profile.objects.get(pk=self.profile.pk)

    def test_saving_one_section_does_not_revert_another_writer(self) -> None:
        """The pin-create signal clears the cached map centre; a settings save must not restore it."""
        Profile.objects.filter(pk=self.profile.pk).update(map_center_latitude=41.5, map_center_longitude=-73.9)
        bound_instance = self._request_snapshot()

        # What invalidate_profile_map_center does when a pin is created.
        Profile.objects.filter(pk=self.profile.pk).update(map_center_latitude=None, map_center_longitude=None)

        form = HistorySettingsForm({"track_routes": "on"}, instance=bound_instance)
        self.assertTrue(form.is_valid(), form.errors)
        form.save()

        self.profile.refresh_from_db()
        self.assertIsNone(
            self.profile.map_center_latitude, "a settings save restored a map centre the pin signal had cleared"
        )
        self.assertIsNone(self.profile.map_center_longitude)

    def test_saving_one_section_does_not_revert_an_import(self) -> None:
        """The importer writes privacy fields by targeted update, over minutes."""
        bound_instance = self._request_snapshot()

        Profile.objects.filter(pk=self.profile.pk).update(allow_friend_recommendations=False)

        form = HistorySettingsForm({"track_routes": "on"}, instance=bound_instance)
        self.assertTrue(form.is_valid(), form.errors)
        form.save()

        self.profile.refresh_from_db()
        self.assertFalse(
            self.profile.allow_friend_recommendations, "a settings save reverted a field the importer had written"
        )

    def test_the_form_still_writes_its_own_fields(self) -> None:
        """Narrowing the write must not narrow it to nothing - in both directions."""
        Profile.objects.filter(pk=self.profile.pk).update(track_routes=False, track_pin_visits=True)

        form = HistorySettingsForm({"track_routes": "on"}, instance=self._request_snapshot())
        self.assertTrue(form.is_valid(), form.errors)
        form.save()

        self.profile.refresh_from_db()
        self.assertTrue(self.profile.track_routes, "a field the form turned on was not written")
        self.assertFalse(self.profile.track_pin_visits, "a field the form turned off was not written")

    def test_every_profile_settings_form_declares_its_fields(self) -> None:
        """The completeness arm.

        The scoped save derives its field list from ``Meta.fields``. A form
        written with ``exclude`` instead would have ``_meta.fields`` of None and
        silently go back to whole-row writes, so that shape has to fail here
        rather than in production.
        """
        undeclared = sorted(form.__name__ for form in ProfileSettingsForm.__subclasses__() if not form._meta.fields)

        self.assertEqual(undeclared, [], "settings forms must list Meta.fields - see ProfileSettingsForm.save")
