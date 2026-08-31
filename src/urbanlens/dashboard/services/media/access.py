"""Who may fetch which file under ``MEDIA_ROOT``.

Every ``/media/...`` request is authorized here, from
:class:`~urbanlens.dashboard.controllers.media.MediaGateView` (browser and
external-API clients alike). The policy lives in a service rather than in the
view so that :mod:`urbanlens.dashboard.checks` can inspect it without importing
a controller, and so a second byte-serving surface can reuse it rather than
restate it.

**Default deny.** A request is authorized only when a *registered* authorizer
for the file's family says yes. The family is the leading path segment, which
is the ``upload_to`` prefix of the model field that wrote the file. An
unregistered family is refused, and ``dashboard.checks.check_media_authorizers``
turns that refusal into a ``manage.py check`` error - so adding a media field
without deciding who may read it breaks the build instead of quietly serving
the files to every logged-in account.

**A file with no owning row is refused too.** An orphan left behind by a
deleted row is indistinguishable from a live file whose owner this viewer is
not allowed to learn about, and guessing which one it is was the hole that
served every thumbnail to everybody: a thumbnail is stored in a second column,
so a lookup against ``image`` alone never matched and every thumbnail took the
permissive orphan branch. Refusing costs nothing - nobody holds a URL for an
orphan except by guessing one.

Authorizers answer about a *stored path*, not a model instance, because a media
URL carries nothing else. They must therefore resolve the owning row from every
field that can hold that path, which for ``Image`` means ``image``,
``thumbnail``, and ``marker_thumbnail``.

Guessing a path is separately useless: the upload-path callables in
:mod:`urbanlens.dashboard.models.images.model` file each upload under a random
directory *and* an opaque filename, so a photo's URL cannot be derived from
either its storage location or the name it was uploaded under. That is
defence in depth, not the control - this module is the control.

Two subtrees of ``MEDIA_ROOT`` hold no model's files and so appear in no
registry: ``exports/<job_id>/`` and ``imports/<job_id>/``, where the data
export and import jobs stage their archives. Both are reached through their own
owner-checked views (``controllers.tools.ExportDownloadView``), never through
``/media/``, so refusing them here is correct - and closes a real hole, since
under the old permissive fallback a guessed job uuid served any user's complete
data export to any logged-in account.
"""

from __future__ import annotations

from collections.abc import Callable
import logging
from typing import TYPE_CHECKING, Final, TypeVar

if TYPE_CHECKING:
    from urbanlens.dashboard.models.profile.model import Profile

logger = logging.getLogger(__name__)

#: An authorizer answers "may *profile* fetch the file stored at *rel_path*?".
#: ``rel_path`` is normalized, traversal-checked, and relative to ``MEDIA_ROOT``.
MediaAuthorizer = Callable[["Profile", str], bool]

_F = TypeVar("_F", bound=Callable[..., str])

#: Attribute a callable ``upload_to`` carries to name the family it writes into.
#: A callable's prefix cannot be read statically, so the check asks the callable
#: to declare it rather than calling it with a fabricated instance.
MEDIA_FAMILY_ATTR: Final = "media_family"

_AUTHORIZERS: dict[str, MediaAuthorizer] = {}


def declares_media_family(prefix: str) -> Callable[[_F], _F]:
    """Mark a callable ``upload_to`` as writing into the *prefix* family.

    Args:
        prefix: The leading path segment the callable's return value starts
            with (e.g. ``"pin_images"``).

    Returns:
        A decorator that annotates the callable and returns it unchanged.
    """

    def annotate(func: _F) -> _F:
        setattr(func, MEDIA_FAMILY_ATTR, prefix)
        return func

    return annotate


def media_authorizer(prefix: str) -> Callable[[MediaAuthorizer], MediaAuthorizer]:
    """Register the decorated function as the authorizer for one path family.

    Args:
        prefix: The ``upload_to`` prefix this authorizer answers for, without
            slashes (e.g. ``"comment_images"``).

    Returns:
        A decorator that registers the function and returns it unchanged.

    Raises:
        RuntimeError: A different authorizer is already registered for
            *prefix*. Two policies for one family means one of them is dead
            code, and which one wins would depend on import order.
    """

    def register(func: MediaAuthorizer) -> MediaAuthorizer:
        existing = _AUTHORIZERS.get(prefix)
        if existing is not None and existing is not func:
            msg = f"Two media authorizers registered for {prefix!r}: {existing.__qualname__} and {func.__qualname__}"
            raise RuntimeError(msg)
        _AUTHORIZERS[prefix] = func
        return func

    return register


def registered_families() -> frozenset[str]:
    """Return every path family that has an authorizer.

    Returns:
        The registered ``upload_to`` prefixes.
    """
    return frozenset(_AUTHORIZERS)


def authorize_media(profile: Profile, rel_path: str) -> bool:
    """Decide whether *profile* may fetch the file at *rel_path*.

    Args:
        profile: The authenticated requester's profile.
        rel_path: Normalized path relative to ``MEDIA_ROOT``, already
            traversal-checked (e.g. ``"pin_images/a7/Kd3xq.../IMG_4821.jpg"``).

    Returns:
        True when the requester may see the file. False for any family without
        a registered authorizer.
    """
    family = rel_path.split("/", 1)[0]
    authorizer = _AUTHORIZERS.get(family)
    if authorizer is None:
        logger.warning("Refused media request for unregistered path family %r", family)
        return False
    return authorizer(profile, rel_path)


@media_authorizer("pin_images")
def authorize_image(profile: Profile, rel_path: str) -> bool:
    """Authorize a ``pin_images/`` file via its ``Image`` row.

    The uploader always qualifies. Direct-message-only attachments are
    restricted to the DM's sender and recipient. Anything else (pin/wiki
    gallery photos, memories uploads, safety check-in photos) follows the same
    ``Image.objects.visible_to`` filtering the gallery views apply.

    All three file columns are searched. A thumbnail (or marker thumbnail) is
    a separate stored file in a separate column that nothing else authorizes,
    so matching only ``image`` left every preview unowned - and therefore,
    under the old permissive orphan fallback, readable by anyone with an
    account.

    Several rows can point at one stored file: accepting a pin share gives the
    recipient their own row over the sender's bytes rather than a second copy,
    and so does re-uploading a file already stored (see ``QuotaExemption``). The
    requester's own row is the one that answers, because any of them would
    otherwise be picked arbitrarily - which for a share recipient means being
    refused a photo they were explicitly given.

    Args:
        profile: The authenticated requester's profile.
        rel_path: Path relative to ``MEDIA_ROOT``.

    Returns:
        True when the requester may see the image.
    """
    from django.db.models import Case, IntegerField, Q, When

    from urbanlens.dashboard.models.images.model import Image

    image = (
        Image.objects.filter(Q(image=rel_path) | Q(thumbnail=rel_path) | Q(marker_thumbnail=rel_path))
        .annotate(requesters_own=Case(When(profile=profile, then=0), default=1, output_field=IntegerField()))
        .select_related("direct_message")
        .order_by("requesters_own")
        .first()
    )
    if image is None:
        return False
    if image.profile_id == profile.pk:
        return True
    if image.pending_scan:
        # Not yet cleared by the malware scan - the stored file is still the
        # uploader's raw bytes (EXIF/GPS intact), so nobody but the uploader may
        # read it. Mirrors authorize_comment_image's identical gate.
        return False
    if image.direct_message_id:
        dm = image.direct_message
        if dm is not None and profile.pk in (dm.sender_id, dm.recipient_id):
            return True
        if not image.pin_id and not image.wiki_id:
            # A pure DM attachment is private to the two participants; an
            # image that *also* lives in a pin/wiki gallery falls through
            # to the normal photo-visibility check below.
            return False
    # pk filter first: `visible_to` eagerly resolves the uploader set of
    # whatever queryset it is handed, so calling it on the unfiltered
    # manager would walk every uploader on the site to answer about one
    # image - on the path that serves every media file.
    return Image.objects.filter(pk=image.pk).visible_to(profile).exists()


@media_authorizer("comment_images")
def authorize_comment_image(profile: Profile, rel_path: str) -> bool:
    """Authorize a ``comment_images/`` file via its Comment/TripComment row.

    The author always qualifies. Everyone else is gated by the author's
    ``comment_visibility`` setting on top of host membership, mirroring the
    gates ``services.comments.comments.visible_comment_tree`` and
    ``services.trips.trip_comments.build_comment_tree`` apply to the comment's
    text - so tightening the setting after a viewer already has the image URL
    revokes access to the file too.

    Args:
        profile: The authenticated requester's profile.
        rel_path: Path relative to ``MEDIA_ROOT``.

    Returns:
        True when the requester may see the comment image.
    """
    from urbanlens.dashboard.models.comments.model import Comment
    from urbanlens.dashboard.models.trips.model import TripComment, TripMembership
    from urbanlens.dashboard.services.wiki.wiki_access import location_visible_to

    comment = Comment.objects.filter(image=rel_path).select_related("pin", "wiki__location", "profile").first()
    if comment is not None:
        if comment.profile_id == profile.pk:
            return True
        if comment.pending_scan:
            # Not yet cleared by the malware scan - author-only until then,
            # mirroring controllers.comments._build_context.
            return False
        if not profile.can_view_comments_from(comment.profile):
            # The author's comment_visibility setting hides the comment itself
            # from this viewer, so the attachment goes with it.
            return False
        if comment.pin is not None:
            # Pin comment threads are visible only on the owner's own pin page
            # (see PinCommentsView.get), so owner + comment authors are the
            # whole audience.
            return comment.pin.profile_id == profile.pk
        if comment.wiki is not None:
            return location_visible_to(comment.wiki.location, profile)
        return False

    trip_comment = TripComment.objects.filter(image=rel_path).select_related("author").first()
    if trip_comment is not None:
        if trip_comment.author_id == profile.pk:
            return True
        if trip_comment.pending_scan:
            return False
        if trip_comment.author is not None and not profile.can_view_comments_from(trip_comment.author):
            return False
        return TripMembership.objects.filter(trip_id=trip_comment.trip_id, profile=profile).exists()

    return False


@media_authorizer("avatars")
def authorize_avatar(profile: Profile, rel_path: str) -> bool:
    """Allow any authenticated user to fetch a profile avatar.

    Avatars render site-wide beside their owner's username - comments, friend
    lists, message threads, leaderboards - so an owner-scoped rule would blank
    most of the site. Uploading one is a deliberate act of publishing a picture
    of yourself to the other members.

    Args:
        profile: The authenticated requester's profile (unused).
        rel_path: Path relative to ``MEDIA_ROOT`` (unused).

    Returns:
        True.
    """
    return True


@media_authorizer("pin_custom_icons")
@media_authorizer("label_icons")
@media_authorizer("achievement_icons")
def authorize_icon(profile: Profile, rel_path: str) -> bool:
    """Allow any authenticated user to fetch a map/label/achievement icon.

    These are decorations drawn next to somebody else's content by design: a
    custom pin icon renders on shared pins and trip member maps, a label icon
    renders wherever that label is applied, and achievement icons are chosen by
    an administrator for everyone. ``Label.objects.visible_to`` is
    global-or-owned, so authorizing through it would blank another member's
    labelled pin.

    TODO(media-auth): an icon is still a user-uploaded image, so this is looser
    than the pixels deserve. Narrowing it needs a "which icons does this viewer
    render" query that does not exist yet; see "Authenticated media gate -
    residual per-family risk" in docs/PROBLEMS.md.

    Args:
        profile: The authenticated requester's profile (unused).
        rel_path: Path relative to ``MEDIA_ROOT`` (unused).

    Returns:
        True.
    """
    return True
