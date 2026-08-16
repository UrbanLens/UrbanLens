"""Any undo handler whose model can own photos must put them back.

``Image`` points at ``Pin``, ``Wiki`` and ``SafetyCheckin`` with ``SET_NULL``, a
deliberate choice: deleting one of those detaches the user's photos rather than
destroying them. The undo handlers did not record which object a photo had been
on, so an undo restored the object *empty* while the photos sat unattached - and
because the FK had already been nulled, the link was unrecoverable.

All three now capture the ids at stash time and re-link on restore, and only
photos that are still detached: one the user has since filed elsewhere stays
where they put it.

The last test is the completeness arm. ``Image`` gaining a fourth ``SET_NULL``
owner with an undo handler would repeat the bug silently, so it is asserted
against the model rather than against a list written today.
"""

from __future__ import annotations

import inspect

from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.images.model import Image
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.models.safety.model import SafetyCheckin
from urbanlens.dashboard.models.wiki.model import Wiki
from urbanlens.dashboard.services.undo.service import restore_undo_action, stash_for_undo

#: model_label -> the ``Image`` FK naming that owner.
_PHOTO_OWNERS = {"pin": "pin", "wiki": "wiki", "safety_checkin": "safety_checkin"}


class UndoPhotoReattachmentTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.profile = Profile.objects.get(user=baker.make("auth.User"))

    def _owner(self, label: str):
        if label == "pin":
            return baker.make(Pin, profile=self.profile, location=baker.make(Location), name="Pin")
        if label == "wiki":
            return baker.make(Wiki, location=baker.make(Location), created_by=self.profile, name="Wiki")
        return baker.make(SafetyCheckin, profile=self.profile, title="Checkin")

    def test_each_owner_gets_its_photos_back(self) -> None:
        for label, fk in _PHOTO_OWNERS.items():
            with self.subTest(owner=label):
                owner = self._owner(label)
                image = baker.make(Image, profile=self.profile, **{fk: owner})

                action = stash_for_undo(label, [owner], self.profile)
                owner.delete()
                image.refresh_from_db()
                self.assertIsNone(getattr(image, f"{fk}_id"), "precondition: the delete detaches rather than destroys")

                restored = restore_undo_action(action)[0]

                image.refresh_from_db()
                self.assertEqual(getattr(image, f"{fk}_id"), restored.pk, f"{label} undo left its photos detached")

    def test_a_photo_refiled_before_the_undo_is_left_alone(self) -> None:
        owner = self._owner("wiki")
        image = baker.make(Image, profile=self.profile, wiki=owner)
        other = self._owner("wiki")
        action = stash_for_undo("wiki", [owner], self.profile)
        owner.delete()
        Image.objects.filter(pk=image.pk).update(wiki=other)

        restore_undo_action(action)

        image.refresh_from_db()
        self.assertEqual(image.wiki_id, other.pk, "undo reclaimed a photo the user had re-filed")

    def test_every_set_null_photo_owner_with_a_handler_is_covered(self) -> None:
        """The completeness arm: a fourth owner must not repeat this silently."""
        from urbanlens.dashboard.services.undo.base import get_handler

        owners = {
            field.related_model.__name__.lower(): field.name
            for field in Image._meta.get_fields()
            if getattr(field, "concrete", False)
            and field.is_relation
            and getattr(getattr(field, "remote_field", None), "on_delete", None) is not None
            and getattr(field.remote_field.on_delete, "__name__", "") == "SET_NULL"
        }

        uncovered = []
        for label in _PHOTO_OWNERS:
            handler = get_handler(label)
            # get_handler returns the class itself, not an instance.
            if "image_ids" not in inspect.getsource(handler if isinstance(handler, type) else type(handler)):
                uncovered.append(label)

        self.assertEqual(uncovered, [], "these undo handlers no longer restore photo attachments")

        # Both directions, deliberately. The subset below catches a *stale*
        # entry - _PHOTO_OWNERS naming a relation Image no longer has. On its
        # own it does not do what this test's docstring promises: a fourth
        # SET_NULL owner arriving with an undo handler keeps _PHOTO_OWNERS a
        # subset, so the guard would pass while the new owner's photos went
        # unrestored - the exact silent repeat it exists to prevent.
        normalised = {name.replace("checkin", "_checkin") if name.endswith("checkin") else name for name in owners}
        self.assertTrue(
            set(_PHOTO_OWNERS).issubset(normalised),
            f"Image gained or lost a SET_NULL owner; reconcile _PHOTO_OWNERS with {sorted(owners)}",
        )

        # The direction that actually enforces completeness: every SET_NULL
        # owner that has an undo handler must be listed. Owners without one
        # (Location, PinVisit, PinSuggestion, DirectMessage today) are outside
        # this test's scope - nothing restores them because nothing undoes them.
        with_handlers = set()
        for name in owners:
            for candidate in {name, name.replace("checkin", "_checkin") if name.endswith("checkin") else name}:
                try:
                    if get_handler(candidate) is not None:
                        with_handlers.add(candidate)
                except Exception:  # noqa: BLE001 - no handler registered under that label
                    continue
        self.assertEqual(
            sorted(with_handlers - set(_PHOTO_OWNERS)),
            [],
            "a SET_NULL photo owner gained an undo handler without being added to _PHOTO_OWNERS - its photos are not being restored",
        )
