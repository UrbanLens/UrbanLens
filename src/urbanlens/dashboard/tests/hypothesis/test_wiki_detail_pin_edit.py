"""Editing and moving a child wiki from its parent wiki's page (P37)."""

from __future__ import annotations

import json
from unittest import mock

from django.contrib.auth.models import User
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.controllers.detail_pins import WikiEdit
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.wiki.model import Wiki


class _ChildWikiCase(TestCase):
    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # the first user is auto-promoted to site admin
        self.user = baker.make(User)
        self.client.force_login(self.user)
        self.location = Location.objects.create(latitude=40.0, longitude=-74.0)
        self.parent_wiki = baker.make_recipe("dashboard.wiki", location=self.location)
        baker.make_recipe("dashboard.pin", profile=self.user.profile, location=self.location)
        self.child = baker.make_recipe(
            "dashboard.wiki",
            parent_wiki=self.parent_wiki,
            location=Location.objects.create(latitude=40.001, longitude=-74.001),
            name="Gatehouse",
            description="Keep this.",
            color="#111111",
        )
        classify = mock.patch("urbanlens.dashboard.controllers.detail_pins._schedule_classification")
        classify.start()
        self.addCleanup(classify.stop)

    def _post(self, body: dict, child: Wiki | None = None, location_slug: str | None = None):
        return self.client.post(
            reverse(
                "location.wiki.detail_pin.edit",
                args=[location_slug or self.location.slug, (child or self.child).uuid],
            ),
            data=json.dumps(body),
            content_type="application/json",
        )


class StyleEditTests(_ChildWikiCase):
    def test_fields_sent_are_saved_and_fields_not_sent_are_kept(self) -> None:
        response = self._post({"name": "Guard House", "color": "#ff0000"})

        self.assertEqual(response.status_code, 200)
        self.child.refresh_from_db()
        self.assertEqual(self.child.name, "Guard House")
        self.assertNotEqual(self.child.color, "#111111")
        self.assertEqual(self.child.description, "Keep this.")
        self.assertFalse(WikiEdit.objects.filter(wiki=self.parent_wiki).exists(), "a style edit wrote an audit entry")

    def test_a_viewer_who_cannot_see_the_wiki_gets_a_404_and_changes_nothing(self) -> None:
        self.client.force_login(baker.make(User))

        response = self._post({"name": "Renamed By A Stranger"})

        self.assertEqual(response.status_code, 404)
        self.child.refresh_from_db()
        self.assertEqual(self.child.name, "Gatehouse")

    def test_a_child_of_a_different_wiki_is_not_reachable_through_this_one(self) -> None:
        other_location = Location.objects.create(latitude=41.0, longitude=-75.0)
        other_parent = baker.make_recipe("dashboard.wiki", location=other_location)
        baker.make_recipe("dashboard.pin", profile=self.user.profile, location=other_location)
        foreign_child = baker.make_recipe(
            "dashboard.wiki",
            parent_wiki=other_parent,
            location=Location.objects.create(latitude=41.001, longitude=-75.001),
            name="Elsewhere",
        )

        response = self._post({"name": "Crossed Over"}, child=foreign_child)

        self.assertEqual(response.status_code, 404)
        foreign_child.refresh_from_db()
        self.assertEqual(foreign_child.name, "Elsewhere")


class MoveTests(_ChildWikiCase):
    def test_a_move_relocates_the_child_and_records_it_on_the_parent(self) -> None:
        response = self._post({"latitude": "40.002", "longitude": "-74.002"})

        self.assertEqual(response.status_code, 200)
        self.child.refresh_from_db()
        self.assertAlmostEqual(float(self.child.location.latitude), 40.002, places=6)
        edit = WikiEdit.objects.get(wiki=self.parent_wiki)
        self.assertEqual(edit.changes["child_wiki_moved"]["pin"], "Gatehouse")

    def test_a_move_onto_another_wikis_point_is_refused_before_anything_is_saved(self) -> None:
        original_location_id = self.child.location_id

        response = self._post({"latitude": "40.0", "longitude": "-74.0", "name": "Should Not Stick"})

        self.assertEqual(response.status_code, 400)
        self.child.refresh_from_db()
        self.assertEqual(self.child.location_id, original_location_id)
        self.assertEqual(self.child.name, "Gatehouse")
        self.assertFalse(WikiEdit.objects.filter(wiki=self.parent_wiki).exists())
