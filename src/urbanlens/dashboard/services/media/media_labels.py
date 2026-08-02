"""Replace the media labels on one photo/video/document, by name.

The site's own label UI (``controllers.labels.LabelImageMembershipView``)
attaches labels one at a time *by id*, from a picker that only ever offers the
viewer's own ``kind='media'`` labels. An API client has no picker and no ids,
so it submits names - which means this module, not the caller, has to enforce
the two invariants the picker enforced implicitly: the labels are media labels,
and they belong to the submitting profile.

Names are matched case-insensitively against the profile's existing media
labels before any row is created, mirroring
``controllers.pin_edit``'s category handling - otherwise every casing variant
a client sends ("Rooftop", "rooftop") would silently accumulate as a separate
label.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from urbanlens.dashboard.models.labels.model import KIND_MEDIA, Label

if TYPE_CHECKING:
    from collections.abc import Sequence

    from urbanlens.dashboard.models.images.model import Image
    from urbanlens.dashboard.models.profile.model import Profile

#: Upper bound on how many media labels one item may carry. Labels exist to
#: make an item findable in search; past this many they stop discriminating,
#: and the cap keeps an automated client from turning one photo into a
#: thousand-row label table.
MAX_MEDIA_LABELS = 25

#: Matches ``Label.name``'s column width - a longer name would be truncated or
#: rejected by the database, so it is refused here with a usable message.
MAX_MEDIA_LABEL_NAME_LENGTH = 255


class MediaLabelError(ValueError):
    """A media-label submission that cannot be applied (too many, blank, or over-long).

    ``safe_message`` is safe to surface directly to the caller.
    """

    def __init__(self, message: str) -> None:
        self.safe_message = message
        super().__init__(message)


def set_media_labels(image: Image, names: Sequence[str], profile: Profile) -> list[Label]:
    """Replace *image*'s media labels with the ones named in *names*.

    This is a replace, not a merge: labels currently on the image that aren't
    named are detached (the ``Label`` rows themselves survive - they may be on
    other items). Passing an empty sequence clears the image's labels.

    Args:
        image: The photo/video/document whose labels to set.
        names: The label names to apply. Whitespace is stripped and
            case-insensitive duplicates collapse to one label.
        profile: The owner the labels are scoped to. Labels are looked up and
            created against this profile only, so a name that matches another
            user's (or a global) label never attaches it here.

    Returns:
        The labels now attached to *image*, in submission order.

    Raises:
        MediaLabelError: More than :data:`MAX_MEDIA_LABELS` names were given,
            or a name was blank or longer than
            :data:`MAX_MEDIA_LABEL_NAME_LENGTH`.
    """
    if len(names) > MAX_MEDIA_LABELS:
        raise MediaLabelError(f"A photo may have at most {MAX_MEDIA_LABELS} labels.")

    cleaned: list[str] = []
    seen: set[str] = set()
    for raw in names:
        name = (raw or "").strip()
        if not name:
            raise MediaLabelError("Label names cannot be blank.")
        if len(name) > MAX_MEDIA_LABEL_NAME_LENGTH:
            raise MediaLabelError(f"Label names cannot exceed {MAX_MEDIA_LABEL_NAME_LENGTH} characters.")
        key = name.casefold()
        if key in seen:
            continue
        seen.add(key)
        cleaned.append(name)

    labels: list[Label] = []
    for name in cleaned:
        # kind and profile are forced, never taken from the caller: a media
        # label must not be able to become (or reuse) a tag/category/status
        # label, which would give it map-icon and filtering effects it is
        # explicitly not supposed to have.
        label = Label.objects.filter(name__iexact=name, kind=KIND_MEDIA, profile=profile).first()
        if label is None:
            label, _created = Label.objects.get_or_create(name=name, kind=KIND_MEDIA, profile=profile)
        labels.append(label)

    image.labels.set(labels)
    return labels
