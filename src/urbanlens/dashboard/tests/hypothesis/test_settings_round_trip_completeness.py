"""Every setting the exporter writes must survive a re-import.

`ImportSettingsCompletenessTests` already round-trips settings, but it names three
fields by hand. A field added to ``_export_settings`` and forgotten in the
importer therefore passes: the user's export contains it, the restore silently
drops it, and the setting reverts to the model default. For a privacy setting
that default is the *more* permissive value.

This is the completeness arm, in the same spirit as
``test_beat_lock_intervals``'s "the scan still finds the known locks": rather than
listing fields, it exports a profile, imports that export into a fresh profile,
and re-exports. Anything the importer ignores shows up as a difference between the
two exports, whatever it is called and whenever it was added.

``_EXPECTED_DIVERGENCES`` records the keys that legitimately do not round-trip, so
that each one is a deliberate, reviewed entry rather than a silent omission.
"""

from __future__ import annotations

import json
import os
import tempfile

from django.contrib.auth.models import User
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.profile.meta import VisibilityChoice
from urbanlens.dashboard.services.import_export import export as export_service, import_data

#: Keys whose value is not expected to survive a round-trip, with the reason.
#: Empty until one is justified - a new entry here should come with an explanation.
_EXPECTED_DIVERGENCES: dict[str, str] = {}


def _read(temp_dir: str, filename: str) -> dict:
    with open(os.path.join(temp_dir, filename), encoding="utf-8") as fh:
        return json.load(fh)


def _flatten(data: dict, prefix: str = "") -> dict:
    """Flatten the grouped settings.json into dotted key -> value."""
    flat: dict = {}
    for key, value in data.items():
        path = f"{prefix}{key}"
        if isinstance(value, dict):
            flat.update(_flatten(value, f"{path}."))
        else:
            flat[path] = value
    return flat


class SettingsRoundTripCompletenessTests(TestCase):
    def _export(self, profile) -> dict:
        with tempfile.TemporaryDirectory() as temp_dir:
            export_service._export_settings(profile, temp_dir)
            return _read(temp_dir, "settings.json")

    def _round_trip(self, source, target) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            export_service._export_settings(source, temp_dir)
            result = import_data.ImportResult()
            import_data._import_settings(target, temp_dir, result, pin_uuid_map={}, label_uuid_map={})
        target.refresh_from_db()

    def _distinctive_profile(self):
        """A profile whose settings differ from the defaults wherever possible.

        Values equal to the default would round-trip "successfully" even if the
        importer ignored them entirely, so the comparison would prove nothing.
        """
        profile = baker.make(User).profile
        for field in profile._meta.get_fields():
            if not getattr(field, "concrete", False) or field.primary_key or field.is_relation:
                continue
            default = field.default
            if callable(default) or default is None:
                continue
            if getattr(field, "choices", None):
                other = [value for value, _label in field.choices if value != default]
                if other:
                    setattr(profile, field.name, other[0])
            elif isinstance(default, bool):
                setattr(profile, field.name, not default)
        # Privacy settings deliberately set to the most restrictive value: those
        # are the ones where a silently-dropped import is a privacy regression.
        for name in ("profile_visibility", "comment_visibility", "common_pins_visibility", "trip_pin_location_visibility"):
            setattr(profile, name, VisibilityChoice.NO_ONE)
        profile.save()
        return profile

    def test_the_fixture_actually_differs_from_a_fresh_profile(self) -> None:
        """Otherwise the round-trip below could pass while importing nothing."""
        source = self._distinctive_profile()
        fresh = baker.make(User).profile

        differing = {k for k, v in _flatten(self._export(source)).items() if _flatten(self._export(fresh)).get(k) != v}

        self.assertGreater(len(differing), 10, f"fixture is too close to default to be meaningful: {sorted(differing)}")

    def test_every_exported_setting_survives_a_re_import(self) -> None:
        """The property: export -> import -> export is idempotent."""
        source = self._distinctive_profile()
        target = baker.make(User).profile

        before = _flatten(self._export(source))
        self._round_trip(source, target)
        after = _flatten(self._export(target))

        dropped = {
            key: (value, after.get(key))
            for key, value in before.items()
            if key not in _EXPECTED_DIVERGENCES and after.get(key) != value
        }

        self.assertEqual(dropped, {}, f"settings exported but not restored (key: exported -> imported): {dropped}")
