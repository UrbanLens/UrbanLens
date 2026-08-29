"""One-liners that stash reversible mutations from write paths.

Call sites pass the live objects; this module builds the payload and
description so each controller does not re-learn the handler's schema.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from urbanlens.dashboard.services.undo.handlers import label_membership as _labels, photo_mutation as _photos, pin_mutation as _pins, wiki_mutation as _wikis
from urbanlens.dashboard.services.undo.service import stash_mutation

if TYPE_CHECKING:
    from collections.abc import Sequence

    from urbanlens.dashboard.models.album.model import Album
    from urbanlens.dashboard.models.aliases.model import PinAlias, WikiAlias
    from urbanlens.dashboard.models.images.model import Image
    from urbanlens.dashboard.models.labels.model import Label
    from urbanlens.dashboard.models.pin.model import Pin
    from urbanlens.dashboard.models.profile.model import Profile
    from urbanlens.dashboard.models.wiki.model import Wiki


def stash_pin_move(pin: Pin, *, before_lat: float, before_lng: float, after_lat: float, after_lng: float) -> None:
    """Record a pin coordinate change, if the point actually moved."""
    if (round(before_lat, 6), round(before_lng, 6)) == (round(after_lat, 6), round(after_lng, 6)):
        return
    stash_mutation(
        _pins.MODEL_LABEL,
        pin.profile,
        payload={
            "op": "move",
            "pin_id": pin.pk,
            "before_lat": before_lat,
            "before_lng": before_lng,
            "after_lat": after_lat,
            "after_lng": after_lng,
        },
        description=f"Moved pin: {pin.effective_name}",
    )


def stash_pin_fields(pin: Pin, *, before: dict[str, Any], after: dict[str, Any]) -> None:
    """Record a pin field edit (name, description, ...)."""
    changed = {key: after[key] for key in after if before.get(key) != after.get(key)}
    if not changed:
        return
    stash_mutation(
        _pins.MODEL_LABEL,
        pin.profile,
        payload={"op": "fields", "pin_id": pin.pk, "before": {key: before.get(key) for key in changed}, "after": changed},
        description=f"Edited pin: {pin.effective_name}",
    )


def stash_pin_alias_add(pin: Pin, alias: PinAlias) -> None:
    """Record adding an alias to a pin."""
    stash_mutation(
        _pins.MODEL_LABEL,
        pin.profile,
        payload={"op": "alias_add", "pin_id": pin.pk, "alias_id": alias.pk, "name": alias.name, "kind": alias.kind},
        description=f"Added alias “{alias.name}”",
    )


def stash_pin_alias_remove(pin: Pin, alias: PinAlias) -> None:
    """Record removing an alias from a pin."""
    stash_mutation(
        _pins.MODEL_LABEL,
        pin.profile,
        payload={"op": "alias_remove", "pin_id": pin.pk, "alias_id": alias.pk, "name": alias.name, "kind": alias.kind},
        description=f"Removed alias “{alias.name}”",
    )


def stash_pin_alias_promote(pin: Pin, *, before_name: str, after_name: str) -> None:
    """Record promoting an alias to the pin's current name."""
    if before_name == after_name:
        return
    stash_mutation(
        _pins.MODEL_LABEL,
        pin.profile,
        payload={"op": "alias_promote", "pin_id": pin.pk, "before_name": before_name, "after_name": after_name},
        description=f"Renamed pin to “{after_name}”",
    )


def stash_wiki_move(wiki: Wiki, profile: Profile, *, before_lat: float, before_lng: float, after_lat: float, after_lng: float) -> None:
    """Record a child-wiki coordinate change."""
    if (round(float(before_lat), 6), round(float(before_lng), 6)) == (round(float(after_lat), 6), round(float(after_lng), 6)):
        return
    stash_mutation(
        _wikis.MODEL_LABEL,
        profile,
        payload={
            "op": "move",
            "wiki_id": wiki.pk,
            "before_lat": float(before_lat),
            "before_lng": float(before_lng),
            "after_lat": float(after_lat),
            "after_lng": float(after_lng),
        },
        description=f"Moved pin: {wiki.name}",
    )


def stash_wiki_alias_add(wiki: Wiki, profile: Profile, alias: WikiAlias) -> None:
    """Record adding an alias to a wiki."""
    stash_mutation(
        _wikis.MODEL_LABEL,
        profile,
        payload={"op": "alias_add", "wiki_id": wiki.pk, "alias_id": alias.pk, "name": alias.name, "kind": alias.kind},
        description=f"Added alias “{alias.name}”",
    )


def stash_wiki_alias_remove(wiki: Wiki, profile: Profile, alias: WikiAlias) -> None:
    """Record removing an alias from a wiki."""
    stash_mutation(
        _wikis.MODEL_LABEL,
        profile,
        payload={"op": "alias_remove", "wiki_id": wiki.pk, "alias_id": alias.pk, "name": alias.name, "kind": alias.kind},
        description=f"Removed alias “{alias.name}”",
    )


def stash_wiki_alias_promote(wiki: Wiki, profile: Profile, *, before_name: str, after_name: str) -> None:
    """Record promoting an alias to the wiki's current name."""
    if before_name == after_name:
        return
    stash_mutation(
        _wikis.MODEL_LABEL,
        profile,
        payload={"op": "alias_promote", "wiki_id": wiki.pk, "before_name": before_name, "after_name": after_name},
        description=f"Renamed to “{after_name}”",
    )


def stash_label_add(profile: Profile, *, target: str, target_id: int, label: Label, created_label: bool = False) -> None:
    """Record applying a label to a pin, wiki, or photo."""
    stash_mutation(
        _labels.MODEL_LABEL,
        profile,
        payload={
            "op": "add",
            "target": target,
            "target_id": target_id,
            "label_id": label.pk,
            "created_label": created_label,
        },
        description=f"Added label “{label.name}”",
    )


def stash_label_remove(profile: Profile, *, target: str, target_id: int, label: Label) -> None:
    """Record removing a label from a pin, wiki, or photo."""
    stash_mutation(
        _labels.MODEL_LABEL,
        profile,
        payload={"op": "remove", "target": target, "target_id": target_id, "label_id": label.pk},
        description=f"Removed label “{label.name}”",
    )


def stash_album_add(profile: Profile, album: Album, image_ids: Sequence[int], *, source_album_id: int | None = None) -> None:
    """Record adding photos to an album.

    Args:
        source_album_id: When this add is a move from another album, undo
            also puts the photos back there.
    """
    ids = [int(image_id) for image_id in image_ids]
    if not ids:
        return
    stash_mutation(
        _photos.MODEL_LABEL,
        profile,
        payload={
            "op": "album_add",
            "album_id": album.pk,
            "image_ids": ids,
            "profile_id": profile.pk,
            "source_album_id": source_album_id,
        },
        description=f"Added {len(ids)} photo{'s' if len(ids) != 1 else ''} to “{album.name}”",
    )


def stash_album_remove(profile: Profile, album: Album, image_ids: Sequence[int]) -> None:
    """Record removing photos from an album."""
    ids = [int(image_id) for image_id in image_ids]
    if not ids:
        return
    stash_mutation(
        _photos.MODEL_LABEL,
        profile,
        payload={"op": "album_remove", "album_id": album.pk, "image_ids": ids, "profile_id": profile.pk},
        description=f"Removed {len(ids)} photo{'s' if len(ids) != 1 else ''} from “{album.name}”",
    )


def stash_photo_fields(profile: Profile, image: Image, *, before: dict[str, Any], after: dict[str, Any]) -> None:
    """Record a photo metadata or map-position change."""
    changed = {key: after[key] for key in after if before.get(key) != after.get(key)}
    if not changed:
        return
    caption = after.get("caption") or before.get("caption") or "photo"
    stash_mutation(
        _photos.MODEL_LABEL,
        profile,
        payload={"op": "fields", "image_id": image.pk, "before": {key: before.get(key) for key in changed}, "after": changed},
        description=f"Edited photo: {caption}",
    )
