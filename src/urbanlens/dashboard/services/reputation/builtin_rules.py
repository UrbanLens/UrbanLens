"""The rules shipped with the ledger.

Deliberately a small set. These are the coarse, hard-to-forge signals that
answer "is this a real participant, or an account minted ninety seconds ago" -
which is all the gate this ledger exists to feed actually needs. The richer
need/quality/amplification scoring the design doc describes improves how
*fairly* contribution is rewarded, and is a later phase.

Several traps here were found by reading the code these rules touch, and are
the reason each rule looks more defensive than its one-line description:

- ``Image.profile`` is **not** the photographer on materialised rows. External
  media becomes an ``Image`` only when somebody up-votes it or sends it to a
  wiki, and the profile on that row is the voter. Bulk imports attach other
  people's photos under the importer. Every photo rule therefore gates on
  ``source == ImageSource.UPLOAD`` rather than trusting ``profile``.
- **Never read ``effective_latitude``** to test for GPS: it falls back to the
  Location's coordinates, so it is never null and proves nothing.
- GPS and EXIF extraction are skipped entirely when the uploader has
  ``track_pin_visits`` off, so metadata is scored as an **additive bonus for
  presence, never a penalty for absence** - otherwise the system quietly pays
  people less for having a privacy setting enabled.
- One Suggest-Edits submit spanning six fields writes **one** ``WikiEdit`` with
  six keys in ``changes``, so a wiki edit is scored by field count, not row
  count.
- ``Wiki.save()`` auto-creates an alias on every rename with
  ``created_by=None``. Those are not contributions and are not scored.
"""

from __future__ import annotations

import datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from django.utils import timezone

from urbanlens.dashboard.models.reputation.meta import TargetKind
from urbanlens.dashboard.services.reputation import coefficients
from urbanlens.dashboard.services.reputation.rules import Rule, ScoreResult, register

if TYPE_CHECKING:
    from urbanlens.dashboard.models.images.model import Image
    from urbanlens.dashboard.models.wiki.model import Wiki

#: Keys that appear in a WikiEdit's `changes` without anyone having typed
#: anything - housekeeping the edit machinery writes for itself.
_UNSCORED_EDIT_KEYS = frozenset({"officially_created"})


def _photo_need(wiki: Wiki | None, image: Image) -> tuple[Decimal, str]:
    """How badly the wiki needed this photo, and why.

    The memo's worked example: nothing at all on the page is worth far more
    than adding to a pile, with "external photos but nothing a user took"
    sitting in between.

    Deliberately counts only *persisted* rows. Establishing whether a wiki has
    transient external photos means walking every gallery panel and hitting a
    provider cache per source, which is the single most expensive input in the
    whole model - and this rule runs for every upload. The cheap approximation
    is to treat materialised external rows as the "has external" signal.
    """
    if wiki is None:
        return coefficients.NEED_ROUTINE, "no_wiki"

    from urbanlens.dashboard.models.images.model import Image as ImageModel, ImageSource, MediaKind

    siblings = ImageModel.objects.filter(wiki=wiki, media_type=MediaKind.PHOTO).exclude(pk=image.pk)
    if not siblings.exists():
        return coefficients.NEED_FIRST_OF_ITS_KIND, "first_photo_at_all"
    if not siblings.filter(source=ImageSource.UPLOAD).exists():
        return coefficients.NEED_FIRST_BY_A_USER, "first_user_photo"
    return coefficients.NEED_ROUTINE, "routine"


def _photo_quality(wiki: Wiki | None, image: Image) -> tuple[Decimal, dict[str, Any]]:
    """Additive metadata and recency bonuses. Never negative."""
    bonus = Decimal(0)
    notes: dict[str, Any] = {}

    if image.taken_at is not None:
        bonus += coefficients.QUALITY_HAS_CAPTURE_DATE
        notes["has_capture_date"] = True
    # Real GPS is latitude/longitude being set. effective_latitude falls back
    # to the Location and would be true for every photo ever uploaded.
    if image.latitude is not None and image.longitude is not None:
        bonus += coefficients.QUALITY_HAS_REAL_GPS
        notes["has_gps"] = True

    if wiki is not None and image.taken_at is not None:
        from django.db.models import Max
        from django.db.models.functions import Coalesce

        from urbanlens.dashboard.models.images.model import Image as ImageModel, MediaKind

        newest = ImageModel.objects.filter(wiki=wiki, media_type=MediaKind.PHOTO).exclude(pk=image.pk).aggregate(newest=Max(Coalesce("taken_at", "created")))["newest"]
        if newest is not None and (image.taken_at - newest) > datetime.timedelta(days=coefficients.TIME_GAP_DAYS):
            bonus += coefficients.QUALITY_CLOSES_TIME_GAP
            notes["closes_time_gap"] = True

    return bonus, notes


def _score_photo(image: Image | None) -> ScoreResult | None:
    """Score a photo somebody uploaded to a wiki."""
    if image is None:
        return None

    from urbanlens.dashboard.models.images.model import ImageSource, MediaKind

    # The profile on a materialised external row is whoever voted for it, not
    # the photographer - so only genuine uploads are contributions.
    if image.source != ImageSource.UPLOAD or image.media_type != MediaKind.PHOTO:
        return None
    if image.wiki_id is None:
        return None

    wiki = image.wiki
    need, need_reason = _photo_need(wiki, image)
    quality, quality_notes = _photo_quality(wiki, image)

    base = coefficients.BASE_VALUES["photo_upload"]
    return ScoreResult(
        value=base * need + quality,
        inputs={"base": str(base), "need": str(need), "need_reason": need_reason, "quality_bonus": str(quality), **quality_notes},
    )


def _score_wiki_edit(edit: Any | None) -> ScoreResult | None:
    """Score a wiki field edit by how many fields it actually changed."""
    if edit is None or edit.reverted:
        return None

    changes = edit.changes if isinstance(edit.changes, dict) else {}
    fields = [key for key in changes if key not in _UNSCORED_EDIT_KEYS]
    if not fields:
        return None

    base = coefficients.BASE_VALUES["wiki_field_edit"]
    return ScoreResult(
        value=base * Decimal(len(fields)),
        inputs={"base": str(base), "field_count": len(fields), "fields": sorted(fields)},
    )


def _score_comment(comment: Any | None) -> ScoreResult | None:
    """Score a comment left on a community wiki."""
    if comment is None or comment.wiki_id is None:
        return None

    from urbanlens.dashboard.models.comments.model import Comment

    siblings = Comment.objects.filter(wiki_id=comment.wiki_id).exclude(pk=comment.pk)
    need = coefficients.NEED_FIRST_OF_ITS_KIND if not siblings.exists() else coefficients.NEED_ROUTINE
    base = coefficients.BASE_VALUES["comment"]
    return ScoreResult(value=base * need, inputs={"base": str(base), "need": str(need)})


def _score_pin(pin: Any | None) -> ScoreResult | None:
    """Score a pin. Low weight - this measures use of the site, not contribution."""
    if pin is None or pin.parent_pin_id is not None:
        return None
    base = coefficients.BASE_VALUES["pin_created"]
    return ScoreResult(value=base, inputs={"base": str(base)})


def _score_invite(invitation: Any | None) -> ScoreResult | None:
    """Score an invitation that was actually accepted."""
    if invitation is None or getattr(invitation, "accepted_at", None) is None:
        return None
    base = coefficients.BASE_VALUES["invite_accepted"]
    return ScoreResult(value=base, inputs={"base": str(base)})


def _score_wiki_created(wiki: Wiki | None) -> ScoreResult | None:
    """Score promoting a place to a real community wiki."""
    if wiki is None or not wiki.officially_created or wiki.created_by_id is None:
        return None
    base = coefficients.BASE_VALUES["wiki_created"]
    return ScoreResult(value=base, inputs={"base": str(base)})


def register_builtin_rules() -> None:
    """Register the shipped rules. Idempotent; called from ``DashboardConfig.ready``."""
    register(
        Rule(
            key="photo_upload",
            label="Photo uploaded to a wiki",
            description="A photo the user took, contributed to a community wiki.",
            target_kind=TargetKind.IMAGE,
            score=_score_photo,
        )
    )
    register(
        Rule(
            key="wiki_field_edit",
            label="Wiki details edited",
            description="A change to a wiki's own fields, scored per field changed.",
            target_kind=TargetKind.WIKI_EDIT,
            score=_score_wiki_edit,
        )
    )
    register(
        Rule(
            key="wiki_comment",
            label="Comment on a wiki",
            description="A comment left on a community wiki.",
            target_kind=TargetKind.COMMENT,
            score=_score_comment,
        )
    )
    register(
        Rule(
            key="pin_created",
            label="Pin created",
            description="A root pin. Measures use of the site rather than contribution.",
            target_kind=TargetKind.PIN,
            score=_score_pin,
        )
    )
    register(
        Rule(
            key="invite_accepted",
            label="Invitation accepted",
            description="Somebody the user invited joined and accepted.",
            target_kind=TargetKind.FRIEND_INVITATION,
            score=_score_invite,
            # Bounded per inviter so vouching cannot be farmed into a supply of
            # sock puppets - see R6 in the design doc.
            capped=True,
        )
    )
    register(
        Rule(
            key="wiki_created",
            label="Community wiki created",
            description="Promoting a place to a real community page.",
            target_kind=TargetKind.WIKI,
            score=_score_wiki_created,
            # Unique per location by construction; nothing to decay.
            decays=False,
        )
    )
