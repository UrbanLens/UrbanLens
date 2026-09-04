"""Applying a consensus answer must write only the field the round decided.

The fourth site of the defect fixed in chunk 617, and the one the manual sweep
missed: ``_wiki_field_strategy.apply_answer`` set one attribute and then
bare-saved the whole ``Wiki``. It was found afterwards by
``bin/report_model_writers.py``, which ranks models by how many modules write
them - ``Wiki`` has thirteen - and lists the bare saves against them.

The staleness window here is wider than the web edit paths that were fixed
first. A consensus round loads its wiki when the round is built and applies the
answer when the round resolves, which is a whole game session later, and rounds
resolve for a wiki that other people are editing in the meantime.

``pin_type`` is the case worth having a test for: its setter assigns *two*
columns, so scoping the save to the strategy's named field alone would have
silently stopped recording ``pin_type_is_user_provided``.
"""

from __future__ import annotations

from django.contrib.auth.models import User
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.consensus.model import ConsensusFieldKind
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.wiki.model import Wiki
from urbanlens.dashboard.services.consensus.fields import get_strategy


class ConsensusApplyAnswerScopeTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # absorbs the bootstrap site-admin promotion
        self.profile = baker.make(User).profile
        self.location = baker.make(Location, latitude=41.0, longitude=-73.0)
        self.wiki = baker.make(Wiki, location=self.location, name="Old Mill", description="Original description")

    def _round_snapshot(self) -> Wiki:
        """The wiki instance a consensus round is holding while it plays out."""
        return Wiki.objects.get(pk=self.wiki.pk)

    def test_applying_an_answer_does_not_revert_a_concurrent_edit(self) -> None:
        stale = self._round_snapshot()

        Wiki.objects.filter(pk=self.wiki.pk).update(description="Edited while the round ran")

        strategy = get_strategy(ConsensusFieldKind.WIKI_NAME)
        strategy.apply_answer(stale, "Mill Complex", self.profile, None)

        self.wiki.refresh_from_db()
        self.assertEqual(self.wiki.name, "Mill Complex", "the answer the round decided did not land")
        self.assertEqual(
            self.wiki.description,
            "Edited while the round ran",
            "applying a consensus answer reverted a concurrent edit",
        )

    def test_applying_an_answer_does_not_reset_another_writers_field(self) -> None:
        stale = self._round_snapshot()

        photo = baker.make("dashboard.Image", wiki=self.wiki)
        Wiki.objects.filter(pk=self.wiki.pk).update(cover_photo=photo)

        strategy = get_strategy(ConsensusFieldKind.WIKI_DESCRIPTION)
        strategy.apply_answer(stale, "Agreed description", self.profile, None)

        self.wiki.refresh_from_db()
        self.assertEqual(self.wiki.description, "Agreed description")
        self.assertEqual(
            self.wiki.cover_photo_id, photo.pk, "a consensus answer reset a field owned by a different writer"
        )

    def test_pin_type_still_records_that_it_was_user_provided(self) -> None:
        """The setter assigns two columns; scoping to the named field alone would drop one."""
        wiki = self._round_snapshot()
        self.assertFalse(wiki.pin_type_is_user_provided)

        strategy = get_strategy(ConsensusFieldKind.WIKI_PIN_TYPE)
        strategy.apply_answer(wiki, "factory", self.profile, None)

        self.wiki.refresh_from_db()
        self.assertEqual(self.wiki.pin_type, "factory")
        self.assertTrue(self.wiki.pin_type_is_user_provided, "the second column the pin-type setter writes was dropped")

    def test_the_recorded_diff_still_describes_the_change(self) -> None:
        """apply_answer's return value feeds the round's audit trail."""
        wiki = self._round_snapshot()

        diff = get_strategy(ConsensusFieldKind.WIKI_NAME).apply_answer(wiki, "Mill Complex", self.profile, None)

        self.assertEqual(diff, {"name": {"from": "Old Mill", "to": "Mill Complex"}})
