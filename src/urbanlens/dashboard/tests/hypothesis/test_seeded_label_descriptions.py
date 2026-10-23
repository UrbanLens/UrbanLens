"""Every label a new profile starts with explains itself on the Organize page, including on profiles seeded before descriptions were."""

from __future__ import annotations

import importlib

from django.apps import apps
from django.contrib.auth.models import User
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.labels.model import Label

_backfill = importlib.import_module("urbanlens.dashboard.migrations.0002_describe_seeded_labels")


class SeededLabelDescriptionTests(TestCase):
    def test_every_seeded_label_has_a_description(self) -> None:
        profile = baker.make(User).profile
        undescribed = list(Label.objects.filter(profile=profile, description__isnull=True).values_list("kind", "name"))
        undescribed += list(Label.objects.filter(profile=profile, description="").values_list("kind", "name"))
        self.assertTrue(Label.objects.filter(profile=profile).exists())
        self.assertEqual(undescribed, [])

    def test_the_backfill_describes_existing_seeded_labels_and_keeps_written_ones(self) -> None:
        profile = baker.make(User).profile
        Label.objects.filter(profile=profile).update(description=None)
        Label.objects.filter(profile=profile, kind="status", name="Visited").update(description="My own words")
        custom = baker.make(Label, profile=profile, kind="tag", name="Rooftop", description=None)

        _backfill.describe_seeded_labels(apps, None)

        self.assertEqual(Label.objects.get(profile=profile, kind="status", name="Visited").description, "My own words")
        self.assertEqual(
            Label.objects.get(profile=profile, kind="category", name="Tunnel").description,
            "Tunnels, drains, sewers and other underground passages.",
        )
        custom.refresh_from_db()
        self.assertIsNone(custom.description)
