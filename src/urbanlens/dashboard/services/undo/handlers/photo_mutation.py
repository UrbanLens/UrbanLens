"""Reversible photo changes: album membership and metadata."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, NoReturn

from urbanlens.dashboard.models.album.model import Album
from urbanlens.dashboard.models.images.model import Image
from urbanlens.dashboard.services.undo.base import MutationUndoHandler, register

if TYPE_CHECKING:
    from urbanlens.dashboard.models.profile.model import Profile

MODEL_LABEL = "photo_mutation"

_PHOTO_FIELDS = ("caption", "author", "copyright", "source_url", "latitude", "longitude", "map_hidden", "taken_at")


def _expired(message: str) -> NoReturn:
    from urbanlens.dashboard.services.undo.service import UndoExpiredError

    raise UndoExpiredError(message)


def _album(album_id: int, profile: Profile) -> Album:
    """The album, only while ``profile`` may still change it: their pin's or Vault's album, or a wiki album they can open."""
    from urbanlens.dashboard.services.wiki.wiki_access import wiki_accessible_to

    album = Album.objects.filter(pk=album_id).select_related("parent_pin", "parent_wiki__location", "parent_wiki__parent_wiki__location").first()
    if album is None:
        _expired("This album no longer exists.")
    if album.parent_pin is not None:
        allowed = album.parent_pin.profile_id == profile.pk
    elif album.parent_wiki is not None:
        allowed = wiki_accessible_to(album.parent_wiki, profile)
    else:
        allowed = album.parent_profile_id == profile.pk
    if not allowed:
        _expired("This album no longer exists.")
    return album


def _own_image(image_id: int | None, profile: Profile) -> Image:
    image = Image.objects.filter(pk=image_id, profile=profile).first()
    if image is None:
        _expired("This photo no longer exists.")
    return image


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
    """Undo/redo album membership and photo metadata changes."""

    model_label = MODEL_LABEL

    @classmethod
    def undo_mutation(cls, payload: dict[str, Any], profile: Profile) -> None:
        from urbanlens.dashboard.services.photos.albums import add_images_to_album, remove_images_from_album

        op = payload.get("op")
        if op == "album_add":
            remove_images_from_album(_album(payload["album_id"], profile), payload["image_ids"])
            source_id = payload.get("source_album_id")
            if source_id:
                add_images_to_album(_album(source_id, profile), _images(payload["image_ids"]), profile)
            return
        if op == "album_remove":
            add_images_to_album(_album(payload["album_id"], profile), _images(payload["image_ids"]), profile)
            return
        if op == "fields":
            _apply_fields(_own_image(payload.get("image_id"), profile), payload.get("before") or {})
            return
        _expired(f"Unknown photo mutation {op!r}.")

    @classmethod
    def redo_mutation(cls, payload: dict[str, Any], profile: Profile) -> None:
        from urbanlens.dashboard.services.photos.albums import add_images_to_album, remove_images_from_album

        op = payload.get("op")
        if op == "album_add":
            source_id = payload.get("source_album_id")
            if source_id:
                remove_images_from_album(_album(source_id, profile), payload["image_ids"])
            add_images_to_album(_album(payload["album_id"], profile), _images(payload["image_ids"]), profile)
            return
        if op == "album_remove":
            remove_images_from_album(_album(payload["album_id"], profile), payload["image_ids"])
            return
        if op == "fields":
            _apply_fields(_own_image(payload.get("image_id"), profile), payload.get("after") or {})
            return
        _expired(f"Unknown photo mutation {op!r}.")
