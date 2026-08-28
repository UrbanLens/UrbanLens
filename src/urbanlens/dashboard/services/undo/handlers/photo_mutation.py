"""Reversible photo changes: album membership, map position, and metadata."""

from __future__ import annotations

from typing import Any

from urbanlens.dashboard.models.album.model import Album
from urbanlens.dashboard.models.images.model import Image
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.services.undo.base import MutationUndoHandler, register

MODEL_LABEL = "photo_mutation"

_PHOTO_FIELDS = ("caption", "author", "copyright", "source_url", "latitude", "longitude", "map_hidden", "taken_at")


def _expired(message: str) -> None:
    from urbanlens.dashboard.services.undo.service import UndoExpiredError

    raise UndoExpiredError(message)


def _album(album_id: int) -> Album:
    album = Album.objects.filter(pk=album_id).first()
    if album is None:
        _expired("This album no longer exists.")
    return album  # type: ignore[return-value]


def _images(image_ids: list[int]) -> list[Image]:
    found = list(Image.objects.filter(pk__in=image_ids))
    if len(found) != len(set(image_ids)):
        _expired("One of the photos in this album change no longer exists.")
    return found


def _apply_fields(image: Image, fields: dict[str, Any]) -> None:
    update = []
    for name, value in fields.items():
        if name not in _PHOTO_FIELDS:
            continue
        setattr(image, name, value)
        update.append(name)
    if update:
        image.save(update_fields=[*update, "updated"])


@register
class PhotoMutationUndoHandler(MutationUndoHandler):
    """Undo/redo album membership and photo metadata/position changes."""

    model_label = MODEL_LABEL

    @classmethod
    def undo_mutation(cls, payload: dict[str, Any]) -> None:
        from urbanlens.dashboard.services.photos.albums import add_images_to_album, remove_images_from_album

        op = payload.get("op")
        if op == "album_add":
            remove_images_from_album(_album(payload["album_id"]), payload["image_ids"])
            source_id = payload.get("source_album_id")
            if source_id:
                profile = Profile.objects.filter(pk=payload.get("profile_id")).first()
                add_images_to_album(_album(source_id), _images(payload["image_ids"]), profile)
            return
        if op == "album_remove":
            profile = Profile.objects.filter(pk=payload.get("profile_id")).first()
            add_images_to_album(_album(payload["album_id"]), _images(payload["image_ids"]), profile)
            return
        if op == "fields":
            image = Image.objects.filter(pk=payload.get("image_id")).first()
            if image is None:
                _expired("This photo no longer exists.")
            _apply_fields(image, payload.get("before") or {})  # type: ignore[arg-type]
            return
        _expired(f"Unknown photo mutation {op!r}.")

    @classmethod
    def redo_mutation(cls, payload: dict[str, Any]) -> None:
        from urbanlens.dashboard.services.photos.albums import add_images_to_album, remove_images_from_album

        op = payload.get("op")
        if op == "album_add":
            profile = Profile.objects.filter(pk=payload.get("profile_id")).first()
            source_id = payload.get("source_album_id")
            if source_id:
                remove_images_from_album(_album(source_id), payload["image_ids"])
            add_images_to_album(_album(payload["album_id"]), _images(payload["image_ids"]), profile)
            return
        if op == "album_remove":
            remove_images_from_album(_album(payload["album_id"]), payload["image_ids"])
            return
        if op == "fields":
            image = Image.objects.filter(pk=payload.get("image_id")).first()
            if image is None:
                _expired("This photo no longer exists.")
            _apply_fields(image, payload.get("after") or {})  # type: ignore[arg-type]
            return
        _expired(f"Unknown photo mutation {op!r}.")
