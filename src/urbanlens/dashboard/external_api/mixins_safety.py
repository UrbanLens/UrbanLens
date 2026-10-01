"""View base for the *watcher* side of a safety check-in.

That question already has exactly one correct implementation,
``services.visits.safety.is_owner_or_accepted_partner``, and this module exists to make sure the API
asks it rather than re-deriving it.
A 403 on a check-in slug would confirm that the slug names a real, currently-active check-in
belonging to someone - i.e. that a specific person is out somewhere - which is a disclosure even
without the check-in's contents, so a check-in the caller may not watch is reported exactly as a
check-in that does not exist.
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

    Deliberately a sibling of ``views.SafetyCheckinScopedView`` rather than a subclass or a replacement.
    The two answer different questions and must keep doing so: widening the owner-scoped base to admit
    partners would silently hand every existing owner-only *write* (edit, cancel, delete, invite) to
    every accepted partner, which is a privilege escalation delivered by a one-line change to a base
    class nobody re-reads.
    """

    def resolve_viewer(self, request: Request) -> Profile:
        """The profile the credential belongs to.

        Args:
            request: The authenticated request.

        Returns:
            The credential owner's profile, created on first use exactly as the session-backed controllers
            do - a user who has never loaded the web...
        """
        profile, _created = Profile.objects.get_or_create(user=request.user)
        return profile

    def get_viewable_checkin(self, request: Request, checkin_slug: str) -> SafetyCheckin | None:
        """The check-in named by *checkin_slug*, if this caller may watch it.

        Expressing it as a queryset filter here would be a second implementation of a rule whose whole risk
        is that a subtly weaker version of it looks correct; the cost is one extra row fetched for a
        check-in the caller cannot watch, and that row never reaches the response.

        Args:
            request: The authenticated request, whose user identifies the viewer.
            checkin_slug: The check-in's slug, or its uuid.

        Returns:
            The check-in, or None when it does not exist or the caller is neither its owner nor an ACCEPTED
            partner on it.
            None with: meth:`not_found` - never with a 403.
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

        Byte-identical to ``views.SafetyCheckinScopedView._not_found`` so that the owner-scoped and
        watcher-scoped surfaces cannot be told apart by their error bodies - a client probing both would
        otherwise learn from the difference which of the two rejected it.

        Returns:
            A 404 response carrying the uniform error envelope.
        """
        return Response({"error": "No such check-in."}, status=404)
