"""The settings UI's preference list must match the model's actual fields.

``services.notifications.notification_center`` introspects
``NotificationPreference`` to derive its stems, and its module docstring says why:
"A hardcoded list here would silently omit it - which is exactly how the
controller's ``_PREF_FIELDS`` and the model can drift apart."

The controller still keeps that hardcoded list, because it carries display labels
the model has no place for. So the drift the docstring warns about is possible,
and nothing was checking for it. Both directions matter and both fail quietly:

- a stem in the model but not in ``_PREF_FIELDS`` is a preference the user can
  never change, though the API exposes it and delivery honours it;
- a name in ``_PREF_FIELDS`` that is not a model field renders a control that
  saves nothing.

This does not require the two to be *generated* from one source - the labels are
a good reason to keep the list - only that they agree.
"""

from __future__ import annotations

from urbanlens.core.tests.testcase import SimpleTestCase
from urbanlens.dashboard.controllers.notifications import _PREF_FIELDS
from urbanlens.dashboard.models.notifications.model import NotificationPreference
from urbanlens.dashboard.services.notifications.notification_center import preference_field_names


def _model_field_names() -> set[str]:
    return {field.name for field in NotificationPreference._meta.get_fields() if getattr(field, "concrete", False)}


class NotificationPreferenceFieldsMatchTests(SimpleTestCase):
    def test_every_listed_field_exists_on_the_model(self) -> None:
        """A control that saves nothing is worse than a missing one."""
        listed = [name for name, _label in _PREF_FIELDS]

        unknown = sorted(set(listed) - _model_field_names())

        self.assertEqual(unknown, [], "the settings page renders preferences that are not model fields")

    def test_every_preference_the_api_exposes_is_editable_in_the_ui(self) -> None:
        """Otherwise a preference is honoured on delivery but unreachable."""
        listed = {name for name, _label in _PREF_FIELDS}

        missing = sorted(set(preference_field_names()) - listed)

        self.assertEqual(missing, [], "preferences exist that the settings page never shows")

    def test_the_list_is_not_empty(self) -> None:
        """Guards the two checks above against passing on an empty comparison."""
        self.assertGreater(len(_PREF_FIELDS), 5)
        self.assertGreater(len(preference_field_names()), 5)

    def test_every_entry_has_a_human_label(self) -> None:
        blank = [name for name, label in _PREF_FIELDS if not (label or "").strip()]

        self.assertEqual(blank, [])

    def test_no_duplicate_entries(self) -> None:
        names = [name for name, _label in _PREF_FIELDS]

        self.assertEqual(len(names), len(set(names)))
