"""External-API mirror of the manual pin <-> wiki child-marker sync.

The detail page's multi-select toolbar can push selected child pins onto the
property's community wiki, and its "pull from wiki" button can create personal
child pins for whatever the wiki already documents. Both are deliberately
manual, opt-in actions (see ``services.pin_wiki_sync`` for why neither ever
runs automatically, and why neither ever *creates* the wiki), and both were
reachable only from the web UI until now.

Three things this module gets right that a naive port would not:

**Ownership is re-established here.** ``services.pin_wiki_sync`` takes a Pin and
trusts it: ``send_pins_to_wiki`` writes child wikis and a ``WikiEdit`` row
attributed to the profile it is handed, and ``pull_children_from_wiki`` creates
pins owned by ``parent_pin.profile``. Neither re-checks that the caller owns the
pin - the internal controllers do it before calling. Resolving through
``OwnedPinMixin`` is therefore not defensive tidiness; skipping it would be a
cross-tenant write.

**A missing wiki is a 200, not a 404.** "This property has no community wiki
yet" is a state a client should render as an invitation, and a 404 here could
not be told apart from "no such pin". The response carries ``wiki_exists`` so
the client can say which of the two zero-result cases it hit.

**Both endpoints stack a resync-grade throttle.** Push is O(children x
buildings) with a GEOS containment test per pair (see
``pin_wiki_sync._find_existing_match``), so one cheap POST from a caller with a
large, densely-pinned property turns into seconds of geospatial work. The
ordinary write cap bounds how *many* writes a credential makes, which is the
wrong question when a single one can cost that much; ``ExternalApiResyncThrottle``
is stacked on top of - not instead of - the standard three, so a sync still
counts against the burst and write budgets as well.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from drf_spectacular.utils import extend_schema
from rest_framework.response import Response

from urbanlens.dashboard.external_api.serializers import ErrorSerializer
from urbanlens.dashboard.external_api.serializers_pin_sync import PinWikiSyncPushSerializer, PinWikiSyncResultSerializer
from urbanlens.dashboard.external_api.throttling import ExternalApiResyncThrottle
from urbanlens.dashboard.external_api.views import ExternalApiView
from urbanlens.dashboard.external_api.views_pin_article import OwnedPinRequiredMixin
from urbanlens.dashboard.models.account.model import ApiKeyScope
from urbanlens.dashboard.models.wiki.model import Wiki
from urbanlens.dashboard.services.pin_wiki_sync import pull_children_from_wiki, send_pins_to_wiki

if TYPE_CHECKING:
    from rest_framework.request import Request


class PinWikiSyncApiView(OwnedPinRequiredMixin, ExternalApiView):
    """Base for the two sync endpoints: own-pin resolution plus the resync throttle."""

    #: Stacked, not substituted: the standard burst/read/write caps still apply,
    #: and this adds the much tighter cap these two endpoints need because their
    #: cost is unbounded in the caller's own data rather than in request count.
    throttle_classes = [*ExternalApiView.throttle_classes, ExternalApiResyncThrottle]

    @staticmethod
    def result(created: int, *, wiki_exists: bool) -> Response:
        """Build the shared ``{created, wiki_exists}`` body.

        Args:
            created: How many markers the service actually created.
            wiki_exists: Whether the pin's location has a community wiki.

        Returns:
            A 200 response carrying both counts.
        """
        return Response({"created": created, "wiki_exists": wiki_exists})


class PinWikiSyncPushView(PinWikiSyncApiView):
    """POST: publish selected child pins to the property's community wiki.

    Requires **both** ``pins:write`` and ``wiki:write``: the call reads the
    caller's private child pins and writes public child wikis plus an
    attributed ``WikiEdit`` row, so consenting to only one half is not consent
    to this. ``HasApiKeyScope`` requires every declared scope, so the frozenset
    below is a genuine AND.
    """

    required_scopes_by_method: ClassVar[dict[str, frozenset[ApiKeyScope]]] = {
        "POST": frozenset({ApiKeyScope.PINS_WRITE, ApiKeyScope.WIKI_WRITE}),
    }

    @extend_schema(request=PinWikiSyncPushSerializer, responses={200: PinWikiSyncResultSerializer, 400: ErrorSerializer, 404: ErrorSerializer})
    def post(self, request: Request, pin_slug: str) -> Response:
        """Create a matching child wiki for each selected child pin not already covered.

        Args:
            request: The authenticated request carrying ``child_pin_uuids``.
            pin_slug: The parent pin's slug or uuid.

        Returns:
            200 with how many child wikis were created, and whether the
            property has a wiki at all.
        """
        pin = self.get_owned_pin_or_404(request, pin_slug)

        serializer = PinWikiSyncPushSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        # Checked up front so the expensive matching pass is skipped entirely
        # for a property with no wiki - the service would do all that work and
        # then return 0 anyway, and the client cannot tell the two zeros apart
        # without this flag.
        if Wiki.objects.get_for_location(pin.location) is None:
            return self.result(0, wiki_exists=False)

        # Scoped to this pin's own direct children, matching the internal
        # toolbar: a uuid naming someone else's pin simply does not match, so
        # the selection cannot be used to publish a pin the caller does not own.
        children = list(pin.detail_pins.filter(uuid__in=serializer.validated_data["child_pin_uuids"]).select_related("location"))
        if not children:
            # A stale selection (the child pins were deleted or re-parented
            # since the client last looked) is a no-op, not an error - same as
            # the internal toolbar, which shows an informational toast.
            return self.result(0, wiki_exists=True)

        created = send_pins_to_wiki(pin, children, pin.profile)
        return self.result(created, wiki_exists=True)


class PinWikiSyncPullView(PinWikiSyncApiView):
    """POST: create personal child pins for the wiki's child wikis not already covered.

    Requires ``pins:write`` (it creates pins) and ``wiki:read`` (it reads the
    community page's markers) - the inverse split from the push endpoint, and
    the reason both scopes are named per direction rather than one "sync" scope.
    """

    required_scopes_by_method: ClassVar[dict[str, frozenset[ApiKeyScope]]] = {
        "POST": frozenset({ApiKeyScope.PINS_WRITE, ApiKeyScope.WIKI_READ}),
    }

    @extend_schema(request=None, responses={200: PinWikiSyncResultSerializer, 404: ErrorSerializer})
    def post(self, request: Request, pin_slug: str) -> Response:
        """Create a personal child pin for each child wiki not already covered.

        Args:
            request: The authenticated request. Takes no body - "pull
                everything the wiki has that I don't" is the whole operation,
                and a per-marker selection would need marker ids the client has
                no other endpoint to enumerate.
            pin_slug: The parent pin's slug or uuid.

        Returns:
            200 with how many child pins were created, and whether the property
            has a wiki at all.
        """
        pin = self.get_owned_pin_or_404(request, pin_slug)
        if Wiki.objects.get_for_location(pin.location) is None:
            return self.result(0, wiki_exists=False)

        created = pull_children_from_wiki(pin)
        return self.result(created, wiki_exists=True)
