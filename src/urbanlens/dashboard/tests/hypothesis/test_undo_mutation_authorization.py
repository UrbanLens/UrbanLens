"""Undo and redo re-check who may touch the object, rather than replaying a stashed primary key."""

from __future__ import annotations

from django.contrib.auth.models import User
from model_bakery import baker
import pytest

from urbanlens.core.tests.labels import ensure_label
from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.aliases.model import PinAlias, WikiAlias
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.models.wiki.model import Wiki
from urbanlens.dashboard.models.wiki_edit import WikiEdit
from urbanlens.dashboard.services.pins.pin_subresources import create_pin_alias
from urbanlens.dashboard.services.undo.mutations import stash_label_add, stash_wiki_alias_add, stash_wiki_alias_promote
from urbanlens.dashboard.services.undo.service import UndoExpiredError, redo_latest, undo_latest


def _profile() -> Profile:
    return Profile.objects.get(user=baker.make(User))


def _placeless_wiki(name: str = "Quiet Mill") -> Wiki:
    location = Location.objects.create(latitude=43.2211, longitude=-72.5533)
    return baker.make(Wiki, location=location, name=name)


class WikiMutationUndoAccessTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.wiki = _placeless_wiki()
        self.editor = _profile()
        self.pin = baker.make(Pin, profile=self.editor, location=self.wiki.location, parent_pin=None)

    def _add_alias(self, name: str = "The Mill") -> WikiAlias:
        alias = WikiAlias.objects.create(wiki=self.wiki, name=name, created_by=self.editor)
        stash_wiki_alias_add(self.wiki, self.editor, alias)
        return alias

    def test_undo_is_refused_once_the_editor_lost_access(self) -> None:
        self._add_alias()
        self.pin.delete()

        with pytest.raises(UndoExpiredError):
            undo_latest(self.editor)

        self.assertTrue(WikiAlias.objects.filter(wiki=self.wiki, name="The Mill").exists())

    def test_redo_is_refused_once_the_editor_lost_access(self) -> None:
        self._add_alias()
        undo_latest(self.editor)
        self.pin.delete()

        with pytest.raises(UndoExpiredError):
            redo_latest(self.editor)

        self.assertFalse(WikiAlias.objects.filter(wiki=self.wiki, name="The Mill").exists())

    def test_undo_and_redo_are_recorded_in_the_wiki_history(self) -> None:
        self._add_alias()

        undo_latest(self.editor)
        undone = WikiEdit.objects.filter(wiki=self.wiki, editor=self.editor).order_by("-pk").first()
        redo_latest(self.editor)
        redone = WikiEdit.objects.filter(wiki=self.wiki, editor=self.editor).order_by("-pk").first()

        assert undone is not None
        assert redone is not None
        self.assertEqual(undone.changes, {"alias_removed": {"from": "The Mill", "to": None}})
        self.assertEqual(redone.changes, {"alias_added": {"from": None, "to": "The Mill"}})

    def test_undoing_a_rename_goes_through_the_wiki_edit_path(self) -> None:
        self.wiki.name = "New Name"
        self.wiki.save(update_fields=["name", "updated"])
        stash_wiki_alias_promote(self.wiki, self.editor, before_name="Quiet Mill", after_name="New Name")

        undo_latest(self.editor)

        self.wiki.refresh_from_db()
        self.assertEqual(self.wiki.name, "Quiet Mill")
        self.assertTrue(WikiEdit.objects.filter(wiki=self.wiki, editor=self.editor, changes__has_key="name").exists())


class PinMutationUndoOwnershipTests(TestCase):
    def test_a_stash_cannot_touch_a_pin_that_now_belongs_to_someone_else(self) -> None:
        owner = _profile()
        pin = baker.make_recipe("dashboard.pin", profile=owner, name="Mill")
        create_pin_alias(pin, name="Old Mill")
        Pin.objects.filter(pk=pin.pk).update(profile=_profile())

        with pytest.raises(UndoExpiredError):
            undo_latest(owner)

        self.assertTrue(PinAlias.objects.filter(pin=pin, name="Old Mill").exists())


class LabelMembershipUndoAccessTests(TestCase):
    def test_a_wiki_label_undo_is_refused_once_the_editor_lost_access(self) -> None:
        wiki = _placeless_wiki()
        editor = _profile()
        pin = baker.make(Pin, profile=editor, location=wiki.location, parent_pin=None)
        label = ensure_label(profile=editor, name="Hazmat", kind="tag")
        wiki.labels.add(label)
        stash_label_add(editor, target="wiki", target_id=wiki.pk, label=label)
        pin.delete()

        with pytest.raises(UndoExpiredError):
            undo_latest(editor)

        self.assertTrue(wiki.labels.filter(pk=label.pk).exists())

    def test_a_pin_label_undo_cannot_reach_another_profiles_pin(self) -> None:
        owner = _profile()
        pin = baker.make_recipe("dashboard.pin", profile=owner)
        label = ensure_label(profile=owner, name="Visited", kind="tag")
        pin.labels.add(label)
        stash_label_add(owner, target="pin", target_id=pin.pk, label=label)
        Pin.objects.filter(pk=pin.pk).update(profile=_profile())

        with pytest.raises(UndoExpiredError):
            undo_latest(owner)

        self.assertTrue(pin.labels.filter(pk=label.pk).exists())
