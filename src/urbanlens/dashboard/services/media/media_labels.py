"""Replace the media labels on one photo/video/document, by name.
The site's own label UI (``controllers.labels.LabelImageMembershipView``) attaches labels one at a time *by id*, from a picker that only ever offers the viewer's own ``kind='media'`` labels."""

from __future__ import annotations

from typing import TYPE_CHECKING

from urbanlens.dashboard.models.labels.meta import KIND_MEDIA
from urbanlens.dashboard.models.labels.model import Label

if TYPE_CHECKING:
    from collections.abc import Sequence

    from urbanlens.dashboard.models.images.model import Image
    from urbanlens.dashboard.models.profile.model import Profile

#: Upper bound on how many media labels one item may carry.
#: Labels exist to make an item findable in search; past this many they stop discriminating, and the
#: cap keeps an automated client from turning one photo into a thousand-row label table.
MAX_MEDIA_LABELS = 25

#: Matches ``Label.name``'s column width - a longer name would be truncated or
#: rejected by the database, so it is refused here with a usable message.
MAX_MEDIA_LABEL_NAME_LENGTH = 255


class MediaLabelError(ValueError):
    """A media-label submission that cannot be applied."""


class TooManyMediaLabelsError(MediaLabelError):
    """More than :data:`MAX_MEDIA_LABELS` names were given for one item."""


class BlankMediaLabelNameError(MediaLabelError):
    """One of the submitted names was empty (or all whitespace)."""


class MediaLabelNameTooLongError(MediaLabelError):
    """One of the submitted names exceeds :data:`MAX_MEDIA_LABEL_NAME_LENGTH`."""


def set_media_labels(image: Image, names: Sequence[str], profile: Profile) -> list[Label]:
    """Replace *image*'s media labels with the ones named in *names*.

    Args:
        image: The photo/video/document whose labels to set.
        names: The label names to apply.
        profile: The owner the labels are scoped to.

    Returns:
        The labels now attached to *image*, in submission order.

    Raises:
        TooManyMediaLabelsError: More than :data:`MAX_MEDIA_LABELS` names were given.
        BlankMediaLabelNameError: One of the names was blank.
        MediaLabelNameTooLongError: One of the names exceeded :data:`MAX_MEDIA_LABEL_NAME_LENGTH`."""
    if len(names) > MAX_MEDIA_LABELS:
        raise TooManyMediaLabelsError(f"Submission had {len(names)} labels, exceeding the cap of {MAX_MEDIA_LABELS}.")

    cleaned: list[str] = []
    seen: set[str] = set()
    for raw in names:
        name = (raw or "").strip()
        if not name:
            raise BlankMediaLabelNameError(f"Blank label name in submission: {names!r}.")
        if len(name) > MAX_MEDIA_LABEL_NAME_LENGTH:
            raise MediaLabelNameTooLongError(f"Label name {len(name)} chars long, exceeding the cap of {MAX_MEDIA_LABEL_NAME_LENGTH}: {name!r}.")
        key = name.casefold()
        if key in seen:
            continue
        seen.add(key)
        cleaned.append(name)

    labels: list[Label] = []
    for name in cleaned:
        # kind and profile are forced, never taken from the caller: a media label must not be able
        # to become (or reuse) a tag/category/status label, which would give it map-icon and
        # filtering effects it is explicitly not supposed to have.
        label = Label.objects.filter(name__iexact=name, kind=KIND_MEDIA, profile=profile).first()
        if label is None:
            label, _created = Label.objects.get_or_create(name=name, kind=KIND_MEDIA, profile=profile)
        labels.append(label)

    image.labels.set(labels)
    return labels
