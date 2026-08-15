"""The chunk-469 export additions: safety, map annotations, saved searches, and the fold-ins.

Closes the remaining uncontroversial half of PROBLEMS.md's export feature-gap
entry. Contact-portal tokens must never reach the archive (a user may forward
it), and secondary emails are exported but deliberately not imported.
"""

from __future__ import annotations

import json
import os
import tempfile

from django.contrib.auth.models import User
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.services.import_export.export import _export_map_annotations, _export_pins, _export_profile, _export_safety, _export_saved_searches
from urbanlens.dashboard.services.import_export.import_data import ImportResult, _import_profile


def _read(temp_dir: str, name: str):
    with open(os.path.join(temp_dir, name), encoding="utf-8") as fh:
        return json.load(fh)


class ExportAdditionsTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        baker.make(User)
        self.profile = baker.make(User).profile

    def test_safety_export_nests_contacts_and_messages_but_never_tokens(self) -> None:
        checkin = baker.make("dashboard.SafetyCheckin", profile=self.profile, title="Mill roof survey", plan_details="in via loading dock")
        contact = baker.make("dashboard.SafetyCheckinContact", checkin=checkin, name="Sam", email="sam@example.test")
        baker.make("dashboard.SafetyCheckinMessage", checkin=checkin, sender_contact=contact, body="be careful up there")

        with tempfile.TemporaryDirectory() as temp_dir:
            _export_safety(self.profile, temp_dir)
            rows = _read(temp_dir, "safety.json")

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["title"], "Mill roof survey")
        self.assertEqual(rows[0]["contacts"][0]["email"], "sam@example.test")
        self.assertEqual(rows[0]["messages"][0]["body"], "be careful up there")
        self.assertNotIn("token", json.dumps(rows), "the contact portal magic-link credential must never reach a forwardable archive")

    def test_map_annotations_export_carries_shapes_and_overlays(self) -> None:
        markup_map = baker.make("dashboard.MarkupMap", profile=self.profile, title="Access sketch")
        baker.make("dashboard.PinMarkup", parent_map=markup_map, profile=self.profile, markup_type="line", geometry={"type": "LineString", "coordinates": [[0, 0], [1, 1]]})
        overlay = baker.make("dashboard.MapImageOverlay", profile=self.profile, name="Sanborn 1897", image_url="https://example.test/sheet.jpg", nw_latitude=1, nw_longitude=0, ne_latitude=1, ne_longitude=1, se_latitude=0, se_longitude=1, sw_latitude=0, sw_longitude=0)

        with tempfile.TemporaryDirectory() as temp_dir:
            _export_map_annotations(self.profile, temp_dir)
            data = _read(temp_dir, "map_annotations.json")

        self.assertEqual(data["markup_maps"][0]["title"], "Access sketch")
        self.assertEqual(data["markup_maps"][0]["markups"][0]["markup_type"], "line")
        self.assertEqual(data["image_overlays"][0]["name"], "Sanborn 1897")
        self.assertEqual(data["image_overlays"][0]["corners"][0], [1.0, 0.0])
        self.assertEqual(str(overlay.uuid), data["image_overlays"][0]["uuid"])

    def test_saved_searches_export_carries_criteria_and_route_geojson(self) -> None:
        baker.make("dashboard.SavedFilter", profile=self.profile, name="ruins near water", criteria={"labels": ["ruin"]})
        from django.contrib.gis.geos import LineString

        baker.make("dashboard.Route", profile=self.profile, name="approach", path=LineString((0, 0), (1, 1), srid=4326))

        with tempfile.TemporaryDirectory() as temp_dir:
            _export_saved_searches(self.profile, temp_dir)
            data = _read(temp_dir, "saved_searches.json")

        self.assertEqual(data["saved_filters"][0]["criteria"], {"labels": ["ruin"]})
        self.assertEqual(data["routes"][0]["path"]["type"], "LineString")

    def test_pin_aliases_ride_in_the_pins_export(self) -> None:
        location = baker.make("dashboard.Location", latitude=40.0, longitude=-74.0)
        pin = baker.make_recipe("dashboard.pin", profile=self.profile, location=location)
        baker.make("dashboard.PinAlias", pin=pin, name="The Old Mill")

        with tempfile.TemporaryDirectory() as temp_dir:
            _export_pins(self.profile, temp_dir)
            rows = _read(temp_dir, "pins.json")

        target = next(row for row in rows if row["uuid"] == str(pin.uuid))
        self.assertEqual(target["aliases"][0]["name"], "The Old Mill")

    def test_social_links_round_trip_and_secondary_emails_do_not(self) -> None:
        baker.make("dashboard.SocialLink", profile=self.profile, platform="instagram", handle="@kay.urbex")
        baker.make("dashboard.ProfileEmail", profile=self.profile, email="second@example.test", normalized_email="second@example.test", is_verified=True)

        with tempfile.TemporaryDirectory() as temp_dir:
            _export_profile(self.profile, temp_dir)
            data = _read(temp_dir, "profile.json")
            self.assertEqual(data["social_links"], [{"platform": "instagram", "handle": "@kay.urbex"}])
            self.assertEqual(data["secondary_emails"][0]["email"], "second@example.test")

            self.profile.social_links.all().delete()
            self.profile.secondary_emails.all().delete()
            _import_profile(self.profile, temp_dir, ImportResult(), pin_uuid_map={}, label_uuid_map={})

        self.assertEqual(self.profile.social_links.count(), 1, "social links should be recreated from the archive")
        self.assertEqual(self.profile.secondary_emails.count(), 0, "secondary emails must NOT be imported - verification state is an account-security decision")
