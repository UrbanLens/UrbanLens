"""View base for the *watcher* side of a safety check-in.

``views.SafetyCheckinScopedView`` is owner-scoped: every endpoint built on it
filters ``SafetyCheckin.objects.filter(profile__user=request.user)``, because
every action it serves (edit, mark safe, cancel, invite, delete) is an owner
action. The partner-facing endpoints are the other half of that feature - a
watcher acknowledging a check-in, escalating an overdue one, or reading the
live position the explorer chose to share - and they need a *different*
question answered: "were you named on this, and did you accept?"

That question already has exactly one correct implementation,
``services.visits.safety.is_owner_or_accepted_partner``, and this module exists to
make sure the API asks it rather than re-deriving it. The re-derivation people
reach for is ``checkin.partners.filter(profile=profile).exists()``, which is
wrong in a way that does not look wrong: ``SafetyCheckinPartner`` rows are
created in the ``INVITED`` state by the invite flow and survive a decline, so
that filter admits someone who was merely *offered* a watching role, or who
explicitly refused it, to a live feed of where another person physically is
right now. There is no more sensitive read in this application.

The uniform 404 discipline matters here for the same reason. A 403 on a
check-in slug would confirm that the slug names a real, currently-active
check-in belonging to someone - i.e. that a specific person is out somewhere -
which is a disclosure even without the check-in's contents, so a check-in the
caller may not watch is reported exactly as a check-in that does not exist.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework.response import Response

from urbanlens.dashboard.external_api.views import ExternalApiView
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.models.safety.model import SafetyCheckin
from urbanlens.dashboard.services.visits.safety import is_owner_or_accepted_partner

if TYPE_CHECKING:
    from rest_framework.request import Request


class SafetyCheckinViewerScopedView(ExternalApiView):
    """Base for endpoints addressing a check-in the caller is entitled to *watch*.

    Resolves a check-in for anyone with a legitimate view of it - its owner, or
    a partner whose invitation is ``ACCEPTED`` - and reports every other case,
    including "no such slug", as an identical 404.

    Deliberately a sibling of ``views.SafetyCheckinScopedView`` rather than a
    subclass or a replacement. The two answer different questions and must keep
    doing so: widening the owner-scoped base to admit partners would silently
    hand every existing owner-only *write* (edit, cancel, delete, invite) to
    every accepted partner, which is a privilege escalation delivered by a
    one-line change to a base class nobody re-reads. Endpoints pick the base
    that matches their authority.

    Subclasses call :meth:`get_viewable_checkin` and answer ``None`` with
    :meth:`not_found`; the two-step shape (rather than raising) mirrors the
    owner-scoped base so both safety surfaces produce the identical error body.
    """

    def resolve_viewer(self, request: Request) -> Profile:
        """The profile the credential belongs to.

        Args:
            request: The authenticated request.

        Returns:
            The credential owner's profile, created on first use exactly as the
            session-backed controllers do - a user who has never loaded the web
            UI still has a valid account, and refusing them here would make the
            API's behaviour depend on whether they had ever visited the site.
        """
        profile, _created = Profile.objects.get_or_create(user=request.user)
        return profile

    def get_viewable_checkin(self, request: Request, checkin_slug: str) -> SafetyCheckin | None:
        """The check-in named by *checkin_slug*, if this caller may watch it.

        The lookup itself is intentionally unscoped and the entitlement check
        is a second step, because the entitlement is a disjunction over two
        different relations (ownership, and an ACCEPTED partner row) that is
        already implemented once in
        ``services.visits.safety.is_owner_or_accepted_partner`` - the same predicate
        the WebSocket consumer and the HTMX chat view gate on. Expressing it as
        a queryset filter here would be a second implementation of a rule whose
        whole risk is that a subtly weaker version of it looks correct; the
        cost is one extra row fetched for a check-in the caller cannot watch,
        and that row never reaches the response.

        Args:
            request: The authenticated request, whose user identifies the viewer.
            checkin_slug: The check-in's slug, or its uuid. Mirrors
                ``views.SafetyCheckinScopedView._get_checkin``: slug first,
                then uuid, since older and directly-linked check-ins are
                addressed by raw uuid. A non-uuid string makes the uuid
                comparison raise rather than simply not match, so that is
                caught and reported as a miss like any other.

        Returns:
            The check-in, or None when it does not exist or the caller is
            neither its owner nor an ACCEPTED partner on it. Callers answer
            None with :meth:`not_found` - never with a 403.
        """
        checkins = SafetyCheckin.objects.select_related("trip", "markup_map", "profile")
        checkin = checkins.filter(slug=checkin_slug).first()
        if checkin is None:
            try:
                checkin = checkins.filter(uuid=checkin_slug).first()
            except (DjangoValidationError, ValueError):
                return None
        if checkin is None:
            return None

        viewer = self.resolve_viewer(request)
        # ACCEPTED-only, and only via the service: see the module docstring for
        # what a bare partners.filter() would let through.
        if not is_owner_or_accepted_partner(checkin, viewer):
            return None
        return checkin

    def not_found(self) -> Response:
        """The single 404 body every check-in lookup miss answers with.

        Byte-identical to ``views.SafetyCheckinScopedView._not_found`` so that
        the owner-scoped and watcher-scoped surfaces cannot be told apart by
        their error bodies - a client probing both would otherwise learn from
        the difference which of the two rejected it.

        Returns:
            A 404 response carrying the uniform error envelope.
        """
        return Response({"error": "No such check-in."}, status=404)
