"""Signal wiring that feeds the reputation ledger.

Every handler does the same three things - decide whether this save is a
contribution, write the row, and queue the scoring - so they are generated from
:data:`_SUBSCRIPTIONS` rather than written out one by one. The shape is lifted
from ``models.achievements.signals``, with two deliberate divergences.

**The write is not deferred; only the scoring is.** Achievements defer
everything to Celery because every metric is a count over other tables and the
nightly sweep can rebuild any of it. The ledger has no such backstop - it *is*
the source of truth - and ``safely_enqueue_task`` returns None on a broker
outage without raising. So the row is written inside the contributor's
transaction (a rolled-back contribution rolls its row back too) and only
``score_reputation_event`` is queued.

**There are retraction handlers.** Achievements have none by design: awards are
never revoked. Here a contribution that gets reverted has to stop counting, and
- because reverting a revert clears ``WikiEdit.reverted`` - has to be able to
start counting again.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import importlib
import logging
from typing import TYPE_CHECKING, Any

from django.db import transaction
from django.db.models.signals import post_save

from urbanlens.dashboard.models.reputation.meta import TargetKind

if TYPE_CHECKING:
    from collections.abc import Callable

    from django.db.models import Model

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _Subscription:
    """How one source model feeds the ledger.

    Attributes:
        model_path: ``module:ClassName`` of the sender, resolved lazily so this
            module can be imported before the app registry is ready.
        rule_key: The rule the saved instance earns, when it qualifies.
        qualifies: Whether this particular instance is a contribution at all.
            Kept separate from the rule's own scoring so an instance that will
            never be worth anything does not get a row written for it.
        profile_id: Whose contribution it is.
        wiki_id: The wiki it landed on, for per-wiki caps. None when there
            isn't one.
        created_only: Skip updates. Off where a later edit is what makes the
            row a contribution - an invitation being accepted, a draft wiki
            being promoted.
    """

    model_path: str
    rule_key: str
    qualifies: Callable[[Any], bool]
    profile_id: Callable[[Any], int | None]
    wiki_id: Callable[[Any], int | None] = lambda _instance: None
    created_only: bool = True


def _is_wiki_upload(image: Any) -> bool:
    """Whether an Image row is a photo its own uploader contributed to a wiki.

    ``profile`` on a materialised external row is whoever up-voted it, not the
    photographer, and a bulk import attaches other people's photos under the
    importer - so the source check is what makes the attribution trustworthy,
    not a refinement of it.
    """
    from urbanlens.dashboard.models.images.model import ImageSource, MediaKind

    return image.wiki_id is not None and image.source == ImageSource.UPLOAD and image.media_type == MediaKind.PHOTO


def _is_scorable_edit(edit: Any) -> bool:
    """Whether a WikiEdit changed anything a person actually typed."""
    changes = edit.changes if isinstance(edit.changes, dict) else {}
    return bool(changes) and not edit.reverted


_SUBSCRIPTIONS: tuple[_Subscription, ...] = (
    _Subscription(
        model_path="urbanlens.dashboard.models.images.model:Image",
        rule_key="photo_upload",
        qualifies=_is_wiki_upload,
        profile_id=lambda image: image.profile_id,
        wiki_id=lambda image: image.wiki_id,
        # Not created_only: a photo is very often attached to its wiki by a
        # later "send to wiki", not at upload.
        created_only=False,
    ),
    _Subscription(
        model_path="urbanlens.dashboard.models.wiki_edit.model:WikiEdit",
        rule_key="wiki_field_edit",
        qualifies=_is_scorable_edit,
        profile_id=lambda edit: edit.editor_id,
        wiki_id=lambda edit: edit.wiki_id,
    ),
    _Subscription(
        model_path="urbanlens.dashboard.models.comments.model:Comment",
        rule_key="wiki_comment",
        qualifies=lambda comment: comment.wiki_id is not None,
        profile_id=lambda comment: comment.profile_id,
        wiki_id=lambda comment: comment.wiki_id,
    ),
    _Subscription(
        model_path="urbanlens.dashboard.models.pin.model:Pin",
        rule_key="pin_created",
        qualifies=lambda pin: pin.parent_pin_id is None,
        profile_id=lambda pin: pin.profile_id,
    ),
    _Subscription(
        model_path="urbanlens.dashboard.models.friendship.invitation.model:FriendInvitation",
        rule_key="invite_accepted",
        qualifies=lambda invitation: invitation.accepted_at is not None,
        profile_id=lambda invitation: invitation.inviter_id,
        # Not created_only: acceptance is an update to the row the invite made.
        created_only=False,
    ),
    _Subscription(
        model_path="urbanlens.dashboard.models.wiki.model:Wiki",
        rule_key="wiki_created",
        qualifies=lambda wiki: wiki.officially_created and wiki.created_by_id is not None,
        profile_id=lambda wiki: wiki.created_by_id,
        wiki_id=lambda wiki: wiki.pk,
        # Not created_only: a draft being promoted is an update.
        created_only=False,
    ),
)


def _resolve(model_path: str) -> type[Model]:
    """Import and return the model named by ``module:ClassName``."""
    module_path, class_name = model_path.split(":")
    return getattr(importlib.import_module(module_path), class_name)


def _make_handler(subscription: _Subscription) -> Callable[..., None]:
    """Build the ``post_save`` receiver for one subscription."""

    def handler(sender: type[Model], instance: Any, created: bool, raw: bool = False, **kwargs: Any) -> None:
        # `raw` is set during loaddata, when related rows may not exist yet.
        if raw or (subscription.created_only and not created):
            return
        if not subscription.qualifies(instance):
            return
        profile_id = subscription.profile_id(instance)
        if profile_id is None:
            return

        from urbanlens.dashboard.services.reputation.scoring import record_event

        event = record_event(profile_id, subscription.rule_key, target=instance, wiki=subscription.wiki_id(instance))
        if event is None:
            return

        def _enqueue() -> None:
            from urbanlens.dashboard.services.core.celery import safely_enqueue_task
            from urbanlens.dashboard.tasks import score_reputation_event

            safely_enqueue_task(score_reputation_event, event.pk)

        transaction.on_commit(_enqueue)

    handler.__name__ = f"reputation_on_{subscription.model_path.rsplit(':', 1)[-1].lower()}_save"
    return handler


def on_wiki_edit_reverted(sender: type[Model], instance: Any, created: bool, raw: bool = False, **kwargs: Any) -> None:
    """Keep a wiki edit's ledger row in step with its ``reverted`` flag.

    Both directions, because ``revert_wiki_edit`` clears the flag when the
    revert is itself reverted - so this is current state, not a one-way
    subtraction.
    """
    if raw or created:
        return

    from urbanlens.dashboard.models.reputation.model import ReputationEvent
    from urbanlens.dashboard.services.reputation.scoring import restore_event, retract_event

    event = ReputationEvent.objects.filter(rule_key="wiki_field_edit", target_kind=TargetKind.WIKI_EDIT, target_id=instance.pk).first()
    if event is None:
        return

    changed = retract_event(event, reason="edit_reverted") if instance.reverted else restore_event(event)
    if not changed:
        return

    def _enqueue() -> None:
        from urbanlens.dashboard.services.core.celery import safely_enqueue_task
        from urbanlens.dashboard.tasks import recompute_reputation_total

        safely_enqueue_task(recompute_reputation_total, event.profile_id)

    transaction.on_commit(_enqueue)


def connect() -> None:
    """Connect every reputation signal. Called from ``DashboardConfig.ready``."""
    for index, subscription in enumerate(_SUBSCRIPTIONS):
        try:
            model = _resolve(subscription.model_path)
        except (ImportError, AttributeError):
            logger.exception("Could not connect reputation signal for %s", subscription.model_path)
            continue
        post_save.connect(
            _make_handler(subscription),
            sender=model,
            # Keyed on the subscription index as well as the model. Django
            # dedupes on (dispatch_uid, sender), so a model-only uid would let
            # a second subscription for a model already listed silently replace
            # the first - one of the two rules would just stop firing, with
            # nothing to notice it.
            dispatch_uid=f"reputation_subscription_{index}_{model._meta.label_lower}",  # noqa: SLF001
            weak=False,
        )

    try:
        from urbanlens.dashboard.models.wiki_edit.model import WikiEdit
    except (ImportError, AttributeError):
        logger.exception("Could not connect the reputation revert handler")
        return
    post_save.connect(on_wiki_edit_reverted, sender=WikiEdit, dispatch_uid="reputation_wiki_edit_reverted", weak=False)
