"""External visit participants: creation from the visit form, email invites, and deferred delivery.

The "Log a Visit" dialog lets a pin owner add two kinds of participants:

- **Members** (their connections): recorded on ``PinVisit.participants``. For
  each, the owner chooses whether to *send* them a visit suggestion; unchecked
  participants are recorded on the owner's own copy of the visit without the
  other user being contacted.
- **External people**: a free-form name plus an optional email address,
  stored as :class:`~urbanlens.dashboard.models.visits.participant.ExternalVisitParticipant`.
  When an email is given and the owner opts to send the suggestion:

  - an address that already belongs to a member delivers a visit suggestion
    and a friend request immediately;
  - an unknown address receives a single join-the-site invitation email
    (subject to the per-user email caps and the one-join-invite-per-address
    rule in ``services.email_safety``), and the friend request + visit
    suggestion are delivered whenever an account with that (primary or
    verified secondary) email appears - even long after the invitation
    email itself expired.

Only a one-way hash of the address is stored (the person hasn't consented to
being in our database); the deferred matching in
:func:`process_pending_visit_invites` compares hashes.
"""

from __future__ import annotations

import logging
import re
import smtplib
from typing import TYPE_CHECKING

from django.core.exceptions import ValidationError
from django.core.mail import EmailMultiAlternatives
from django.core.validators import validate_email
from django.template.loader import render_to_string

from urbanlens.dashboard.models.email_log import EmailType
from urbanlens.dashboard.models.visits.participant import ExternalVisitParticipant
from urbanlens.dashboard.services.email_normalization import find_user_by_email
from urbanlens.dashboard.services.email_safety import email_rate_limit_error, has_sent_join_email, hash_email, record_email_sent

if TYPE_CHECKING:
    from django.contrib.auth.models import User
    from django.http import HttpRequest

    from urbanlens.dashboard.models.profile.model import Profile
    from urbanlens.dashboard.models.visits.model import PinVisit

logger = logging.getLogger(__name__)

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


def _send_visit_invite_email(request: HttpRequest, owner: Profile, email: str) -> bool:
    """Send the join-the-site email for a visit invite, honouring all safety rules.

    Args:
        request: Current request (used to build the absolute signup URL).
        owner: The pin owner triggering the invite.
        email: The recipient address.

    Returns:
        True when the email was actually sent.
    """
    if has_sent_join_email(owner, email):
        return False
    if email_rate_limit_error(owner):
        logger.info("Visit invite email suppressed by rate limit for profile %s", owner.pk)
        return False

    # A FriendInvitation supplies the tokenised signup link (required on
    # invite-only sites) and near-term auto-friending; long-term matching is
    # handled by the hashed ExternalVisitParticipant row instead.
    from urbanlens.dashboard.models.friendship.invitation import FriendInvitation

    FriendInvitation.objects.filter(inviter=owner, email=email, accepted_at__isnull=True).delete()
    invitation = FriendInvitation(inviter=owner, email=email)
    invitation.save()

    signup_url = request.build_absolute_uri(f"/signup/?invite={invitation.token}")
    context = {"inviter": owner, "signup_url": signup_url}
    subject = f"{owner.username} invited you to join UrbanLens"
    text_body = (
        f"Hi,\n\n{owner.username} logged a visit with you on UrbanLens - a private mapping platform "
        f"for urban explorers and photographers - and invited you to join.\n\nAccept the invitation:\n{signup_url}\n\n- UrbanLens"
    )
    html_body = render_to_string("dashboard/email/friend_invite.html", context)

    try:
        message = EmailMultiAlternatives(subject=subject, body=text_body, from_email=None, to=[email])
        message.attach_alternative(html_body, "text/html")
        message.send()
    except (smtplib.SMTPException, OSError):
        logger.exception("Failed to send visit invitation email for profile %s", owner.pk)
        return False

    record_email_sent(owner, email, EmailType.VISIT_INVITE)
    return True


def _deliver_to_member(owner: Profile, member: Profile, visit: PinVisit) -> None:
    """Send the friend request + visit suggestion for a matched member.

    Args:
        owner: The pin owner who logged the visit.
        member: The member the external participant resolved to.
        visit: The visit the person took part in.
    """
    from urbanlens.dashboard.controllers.friendship import request_or_accept_friendship
    from urbanlens.dashboard.services.visits import create_visit_suggestion

    if member.pk == owner.pk:
        return
    request_or_accept_friendship(owner, member)

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


def _handle_external_email(request: HttpRequest, participant: ExternalVisitParticipant, email: str) -> None:
    """Resolve one external participant's email: member match or join invite.

    Args:
        request: Current request (for signup URL building).
        participant: The freshly created external participant row.
        email: The raw email the owner entered (hashed, never stored).
    """
    owner = participant.visit.pin.profile
    existing_user = find_user_by_email(email)
    if existing_user is not None:
        participant.matched_profile = existing_user.profile
        participant.save(update_fields=["matched_profile", "updated"])
        if participant.suggestion_requested:
            _deliver_to_member(owner, existing_user.profile, participant.visit)
        return

    if participant.suggestion_requested:
        participant.invite_sent = _send_visit_invite_email(request, owner, email)
        participant.save(update_fields=["invite_sent", "updated"])


def sync_external_participants(request: HttpRequest, visit: PinVisit) -> None:
    """Create/remove external participants for a visit from the submitted form.

    The form submits indexed field groups (``external_name_N``,
    ``external_email_N``, ``external_invite_N``) for new people, and
    ``external_remove`` ids for existing rows to drop.

    Args:
        request: Request carrying the visit form.
        visit: The visit being created or edited.
    """
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
        if email:
            _handle_external_email(request, participant, email)


def process_pending_visit_invites(user: User, email: str | None = None) -> int:
    """Deliver deferred friend requests + visit suggestions for a (newly verified) email.

    Called when a new account's email is verified and when a member verifies
    an additional (secondary) address: any external visit participants whose
    hashed email matches are resolved to this account, and - where the visit
    owner asked for it - the friend request and visit suggestion are sent.

    Args:
        user: The account the email belongs to.
        email: The specific address that was just verified; defaults to the
            account's primary email.

    Returns:
        The number of participant rows resolved to this account.
    """
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
