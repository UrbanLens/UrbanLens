"""External visit participants: creation from the visit form, email invites, and deferred delivery.

What the owner sees of a tagged address never depends on whether it has an account: the friendship is offered
through a ``FriendInvitation`` and the visit through a ``VisitSuggestion``, each answered by the invitee separately.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.db import transaction

from urbanlens.dashboard.models.visits.participant import ExternalVisitParticipant
from urbanlens.dashboard.services.auth.email_normalization import find_verified_user_by_email
from urbanlens.dashboard.services.security.email_safety import hash_email

if TYPE_CHECKING:
    from django.contrib.auth.models import User
    from django.http import HttpRequest

    from urbanlens.dashboard.models.profile.model import Profile
    from urbanlens.dashboard.models.visits.model import PinVisit

_EXTERNAL_NAME_FIELD = re.compile(r"^external_name_(\d+)$")
_MAX_EXTERNAL_PARTICIPANTS_PER_VISIT = 25


def resolve_suggest_participant_ids(request: HttpRequest) -> set[int]:
    """Member participant ids the owner wants visit suggestions sent to.

    Args:
        request: Request carrying ``suggest_participant_ids`` checkboxes.

    Returns:
        The set of profile ids whose suggestion checkbox was left on.
    """
    return {int(pid) for pid in request.POST.getlist("suggest_participant_ids") if pid.strip().isdigit()}


def _deliver_to_member(owner: Profile, member: Profile, visit: PinVisit) -> None:
    """Offer ``member`` the visit as a suggestion they can accept or decline.

    Args:
        owner: The pin owner who logged the visit.
        member: The member the external participant resolved to.
        visit: The visit the person took part in.
    """
    from urbanlens.dashboard.models.friendship import Friendship, FriendshipStatus
    from urbanlens.dashboard.services.visits.visits import create_visit_suggestion

    if member.pk == owner.pk:
        return
    existing = Friendship.objects.all().between(owner, member)
    if existing is not None and existing.status == FriendshipStatus.BLOCKED:
        return

    pin = visit.pin
    latitude, longitude = pin.effective_latitude, pin.effective_longitude
    if latitude is None or longitude is None:
        return
    create_visit_suggestion(
        suggested_to=member,
        suggested_by=owner,
        visited_at=visit.visited_at,
        location=pin.location,
        latitude=latitude,
        longitude=longitude,
        candidate_profiles=[],
        origin_visit=visit,
        origin_pin=pin,
    )


def _invite(request: HttpRequest, participant: ExternalVisitParticipant, email: str) -> None:
    """Invite a tagged person by email: the friendship through a friend invitation, the visit once an account is proven to own the address.

    The request does the same for every address; which of the two applies is decided in ``deliver_to_participant``.

    Args:
        request: Current request (for building absolute URLs).
        participant: The freshly created external participant row.
        email: The raw email the owner entered.
    """
    from urbanlens.dashboard.services.social.friendship import InviteRateLimitedError, InviteValidationError, invite_by_email

    try:
        invitation = invite_by_email(participant.visit.pin.profile, email, url_builder=request.build_absolute_uri)
    except (InviteValidationError, InviteRateLimitedError):
        return
    participant.invite_sent = True
    participant.save(update_fields=["invite_sent", "updated"])
    transaction.on_commit(lambda: _queue_delivery(participant.pk, invitation.pk))


def _queue_delivery(participant_id: int, invitation_id: int) -> None:
    from urbanlens.dashboard.services.core.celery import safely_enqueue_task
    from urbanlens.dashboard.tasks import deliver_visit_invite

    safely_enqueue_task(deliver_visit_invite, participant_id, invitation_id)


def deliver_to_participant(participant_id: int, invitation_id: int) -> None:
    """Offer the visit to the account proven to own the invited address; an address without one waits for signup.

    Args:
        participant_id: PK of the ExternalVisitParticipant.
        invitation_id: PK of the FriendInvitation holding its address.
    """
    from urbanlens.dashboard.models.friendship.invitation import FriendInvitation

    participant = ExternalVisitParticipant.objects.filter(pk=participant_id, matched_profile__isnull=True).select_related("visit__pin__profile", "visit__pin__location").first()
    invitation = FriendInvitation.objects.filter(pk=invitation_id).first()
    if participant is None or invitation is None:
        return
    account = find_verified_user_by_email(invitation.email)
    if account is None:
        return
    participant.matched_profile = account.profile
    participant.save(update_fields=["matched_profile", "updated"])
    if participant.suggestion_requested:
        _deliver_to_member(participant.visit.pin.profile, account.profile, participant.visit)


def sync_external_participants(request: HttpRequest, visit: PinVisit) -> None:
    """Create/remove external participants for a visit from the submitted form.

    Args:
        request: Request carrying the visit form.
        visit: The visit being created or edited."""
    remove_ids = {int(pid) for pid in request.POST.getlist("external_remove") if pid.strip().isdigit()}
    if remove_ids:
        ExternalVisitParticipant.objects.filter(visit=visit, pk__in=remove_ids).delete()

    existing_count = ExternalVisitParticipant.objects.filter(visit=visit).count()
    for key in request.POST:
        match = _EXTERNAL_NAME_FIELD.match(key)
        if not match:
            continue
        if existing_count >= _MAX_EXTERNAL_PARTICIPANTS_PER_VISIT:
            break
        index = match.group(1)
        name = request.POST.get(key, "").strip()
        if not name:
            continue
        email = request.POST.get(f"external_email_{index}", "").strip().lower()
        wants_suggestion = request.POST.get(f"external_invite_{index}") in {"1", "on", "true"}
        if email:
            try:
                validate_email(email)
            except ValidationError:
                email = ""

        participant = ExternalVisitParticipant.objects.create(
            visit=visit,
            display_name=name[:100],
            email_hash=hash_email(email) if email else "",
            suggestion_requested=bool(email) and wants_suggestion,
        )
        existing_count += 1
        if participant.suggestion_requested:
            _invite(request, participant, email)


def process_pending_visit_invites(user: User, email: str | None = None) -> int:
    """Deliver deferred friend requests + visit suggestions for a (newly verified) email.

    Args:
        user: The account the email belongs to.
        email: The specific address that was just verified; defaults to the account's primary email.

    Returns:
        The number of participant rows resolved to this account."""
    from urbanlens.dashboard.models.profile.model import Profile

    address = (email or user.email or "").strip()
    if not address:
        return 0

    profile, _ = Profile.objects.get_or_create(user=user)
    matches = ExternalVisitParticipant.objects.filter(
        email_hash=hash_email(address),
        matched_profile__isnull=True,
    ).select_related("visit__pin__profile", "visit__pin__location")

    resolved = 0
    for participant in matches:
        participant.matched_profile = profile
        participant.save(update_fields=["matched_profile", "updated"])
        resolved += 1
        if participant.suggestion_requested:
            _deliver_to_member(participant.visit.pin.profile, profile, participant.visit)
    return resolved
