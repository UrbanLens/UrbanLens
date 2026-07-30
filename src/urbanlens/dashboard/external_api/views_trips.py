"""External-API views for trip settings and Google Calendar export.

Two endpoints that the trip surface was missing entirely, for two different
reasons.

**Settings had no write path at all.** The four permission levels were
readable in the trip detail payload and editable only through the site's own
HTMX form, so a native client could show a trip's rules but never change them.
The gap was not merely a missing route: the underlying service reset every
field the caller did not mention, which is harmless behind a form that always
submits all four and destructive behind a partial-update endpoint. That was
fixed in ``services.trip_crud`` first; this module is what stands on it.

**Export was assumed to be browser-only.** It is not. A user's Google tokens
are stored per profile *with a refresh token*, and ``GoogleCalendarGateway``
refreshes them without any user interaction - so an export made from an API
credential works exactly as well as one made from a browser tab. Only the
one-time consent is browser-bound, which is why the 409 here hands back the
**site's own** connect route rather than a Google URL: a Google authorization
URL minted by this API would come back to the OAuth callback carrying no
session, redirect to the login page, and drop the authorization code.

Three constraints worth stating because each is easy to undo:

- **No transaction around the calendar calls.** ``export_trip_to_calendar``
  makes one upstream request per event, so wrapping it in ``atomic`` would hold
  a database connection open across an unbounded third-party fan-out. The
  service is idempotent instead - a retry updates rather than duplicates.
- **The settings endpoint answers 403, not 404, to a non-organizer member.**
  That is a deliberate departure from this package's usual rule. The rule
  exists so a caller cannot learn that a resource they may not touch *exists*;
  a member has already been shown the trip in full, so telling them their role
  is insufficient reveals nothing they did not already have. A non-member still
  gets 404, from the shared trip lookup, exactly like everyone else.
- **The calendar routes carry their own throttle bucket.** They are the only
  endpoints in this package that talk to a third party on the request path, and
  the quota being spent is the deployment's, not just the caller's.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar

from django.urls import reverse
from drf_spectacular.utils import extend_schema
from rest_framework.response import Response

from urbanlens.dashboard.external_api.serializers import ErrorSerializer, TripDetailSerializer
from urbanlens.dashboard.external_api.serializers_trips import (
    TripCalendarBlockedSerializer,
    TripCalendarExportRequestSerializer,
    TripCalendarExportResponseSerializer,
    TripCalendarRemovalResponseSerializer,
    TripCalendarStatusSerializer,
    TripPermissionsUpdateSerializer,
)
from urbanlens.dashboard.external_api.throttling import (
    CalendarExportThrottle,
    ExternalApiBurstThrottle,
    ExternalApiReadThrottle,
    ExternalApiWriteThrottle,
)

# ``_trip_detail_payload`` decorates a Trip with the viewer/calendar/members
# blocks ``TripDetailSerializer`` reads. It is imported rather than reproduced
# because the settings endpoint's whole value is answering with a *recomputed*
# detail payload - a second assembly of it would drift from the one every other
# trip endpoint returns, and the divergence would show up as a client whose
# viewer.can_* flags disagree with themselves depending on which call produced
# them. The leading underscore is a wart: it belongs in a service alongside the
# other trip read models, which is a refactor of ``views.py`` (owned elsewhere)
# rather than of this module.
from urbanlens.dashboard.external_api.views import TripScopedApiView, _trip_detail_payload
from urbanlens.dashboard.models.calendar_sync.model import GoogleCalendarAccount, TripCalendarLink
from urbanlens.dashboard.services.apis.calendar.google import CalendarNotConfiguredError
from urbanlens.dashboard.services.calendar_sync import export_trip_to_calendar, remove_trip_from_calendar, trip_calendar_status
from urbanlens.dashboard.services.gateway import GatewayRequestError
from urbanlens.dashboard.services.google_oauth import GoogleAuthExpiredError
from urbanlens.dashboard.services.trip_crud import set_trip_permissions
from urbanlens.dashboard.services.trip_errors import TripError

if TYPE_CHECKING:
    from rest_framework.request import Request

    from urbanlens.dashboard.models.profile.model import Profile
    from urbanlens.dashboard.models.trips.model import Trip

#: Machine-readable codes for the two browser-bound calendar refusals. Both are
#: resolved by the same consent flow; they are distinguished so a client can
#: word the prompt correctly without parsing prose.
CALENDAR_NOT_CONNECTED = "calendar_not_connected"
CALENDAR_REAUTHORIZATION_REQUIRED = "calendar_reauthorization_required"

#: Which of the site's post-connect landing pages the consent flow returns to.
#: ``GoogleCalendarConnectView`` validates this against an allow-list, so an
#: arbitrary value would be silently replaced rather than honoured - it is
#: named here so the pairing is visible from this side too.
_CONNECT_NEXT_VIEW = "trips.list"

_NOT_CONNECTED_MESSAGE = "Connect your Google Calendar before exporting a trip to it."
_REAUTHORIZATION_MESSAGE = "Your Google Calendar connection has expired. Reconnect it to keep exporting trips."
_NOT_CONFIGURED_MESSAGE = "Google Calendar integration is not configured on this server."


class TripSettingsView(TripScopedApiView):
    """PATCH: change one or more of a trip's four permission levels.

    Answers with the full trip detail rather than a bare acknowledgement,
    because the levels are not what a client renders from - the derived
    ``viewer.can_*`` booleans are, and those depend on the caller's role as
    well as on the levels. Returning the recomputed detail means one round trip
    instead of a write followed by a re-read that a client could forget to
    make, leaving its buttons enabled for actions the server would now refuse.
    """

    @extend_schema(
        request=TripPermissionsUpdateSerializer,
        responses={200: TripDetailSerializer, 400: ErrorSerializer, 403: ErrorSerializer, 404: ErrorSerializer},
    )
    def patch(self, request: Request, trip_slug: str) -> Response:
        """Apply a presence-keyed permission update and return the refreshed trip.

        Args:
            request: The authenticated request.
            trip_slug: The trip's URL slug.

        Returns:
            200 with the full trip detail; 400 for an invalid or empty
            submission; 403 when the caller is a member but not an organizer;
            404 when the trip does not exist *or* is not one this caller is on.
        """
        serializer = TripPermissionsUpdateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        profile = request.user.profile
        try:
            # self.trip() raises TripNotFoundError -> 404 for a trip this caller
            # is not on, so the 403 below can only ever reach somebody who has
            # already been shown this trip.
            trip = set_trip_permissions(self.trip(request, trip_slug), profile, changes=serializer.validated_data)
        except TripError as exc:
            return self.error_response(exc)
        return Response(TripDetailSerializer(_trip_detail_payload(trip, profile), context={"viewer": profile}).data)


class TripCalendarExportView(TripScopedApiView):
    """POST: mirror a trip onto the caller's Google Calendar. DELETE: take it back off.

    Both directions operate on the *caller's own* calendar - each member of a
    shared trip exports to their own, and one member's export says nothing
    about anyone else's. Locations are filtered per exporter before anything is
    written (see ``services.calendar_sync.export_trip_to_calendar``): a trip
    carries other people's stops, and their ``trip_pin_location_visibility``
    settings decide what may leave the site on this caller's behalf.

    The status block returned by both methods is the same shape the trip detail
    payload embeds, so a client can drop it straight into its cached trip
    without a re-fetch.
    """

    #: The standard three plus the calendar bucket. Not a replacement for them:
    #: an export still costs a write, and should still count as one.
    throttle_classes = [ExternalApiBurstThrottle, ExternalApiReadThrottle, ExternalApiWriteThrottle, CalendarExportThrottle]

    def _blocked(self, request: Request, *, error_code: str, message: str) -> Response:
        """Answer 409 with the site route that re-establishes consent.

        Args:
            request: The authenticated request, used to absolutize the URL.
            error_code: One of :data:`CALENDAR_NOT_CONNECTED` or
                :data:`CALENDAR_REAUTHORIZATION_REQUIRED`.
            message: Human-readable explanation for the client to surface.

        Returns:
            A 409 carrying the :class:`TripCalendarBlockedSerializer` shape.
        """
        # Absolute, because the client is expected to hand this to a *system
        # browser* rather than to its own HTTP stack - a bare path has nothing
        # to resolve against once it leaves the app.
        authorization_url = request.build_absolute_uri(f"{reverse('trips.calendar.connect')}?next={_CONNECT_NEXT_VIEW}")
        return Response({"error": message, "error_code": error_code, "authorization_url": authorization_url}, status=409)

    def _account_or_blocked(self, request: Request) -> GoogleCalendarAccount | Response:
        """Resolve the caller's calendar account, or the 409 explaining its absence.

        Returns the response in place of the account - the same shape
        ``controllers.trip.trip_or_not_found`` uses - so the caller's guard is
        a single ``isinstance`` rather than a two-value unpack whose second
        element is None in the success case and has to be narrowed anyway.

        Args:
            request: The authenticated request.

        Returns:
            The connected account, or a 409 ``Response`` when there is none.
        """
        account = GoogleCalendarAccount.objects.get_for_profile(request.user.profile)
        if account is None:
            return self._blocked(request, error_code=CALENDAR_NOT_CONNECTED, message=_NOT_CONNECTED_MESSAGE)
        return account

    def _gateway_failure(self, request: Request, account: GoogleCalendarAccount, exc: Exception) -> Response:
        """Translate a calendar failure into the right status for a client.

        A dead grant is handled by *deleting* the stored account first. Keeping
        a connection Google has already rejected around would make every
        subsequent request repeat the identical failure, and would leave the
        trip detail payload reporting ``connected: true`` for a calendar that
        can no longer be written to - which is worse than reporting nothing,
        because a client would render a working export button.

        Args:
            request: The authenticated request.
            account: The account the failed call was made with.
            exc: The exception raised by the calendar service.

        Returns:
            409 for a dead grant, 503 when the deployment has no OAuth client
            configured, 502 for any other upstream failure.
        """
        if isinstance(exc, GoogleAuthExpiredError):
            account.delete()
            return self._blocked(request, error_code=CALENDAR_REAUTHORIZATION_REQUIRED, message=_REAUTHORIZATION_MESSAGE)
        if isinstance(exc, CalendarNotConfiguredError):
            # A deployment-level omission, not anything the caller did wrong.
            return Response({"error": _NOT_CONFIGURED_MESSAGE}, status=503)
        # Only a GatewayRequestError reaches here (the other two members of the
        # except clause above are handled by the isinstance checks); every
        # raise site constructs it from developer-authored, status-code-only
        # text, never from an interpolated upstream exception/response body.
        return Response({"error": str(exc)}, status=502)  # lgtm[py/stack-trace-exposure]

    def _status_response(self, trip: Trip, profile: Profile, extra: dict[str, Any]) -> Response:
        """Answer 200 with the refreshed status block plus this method's summary.

        The status is re-read rather than assembled from what the write
        returned, so ``auto_sync`` and ``last_synced`` reflect the row as it
        now stands - including the auto-sync flag POST may have changed after
        the export itself completed.

        Args:
            trip: The trip that was just exported or unexported.
            profile: The caller, whose calendar the status describes.
            extra: The method-specific keys to merge in
                (``activities_exported`` or ``removed``).

        Returns:
            A 200 response.
        """
        status = trip_calendar_status(trip, profile)
        return Response({"calendar": TripCalendarStatusSerializer(status).data, **extra})

    @extend_schema(
        request=TripCalendarExportRequestSerializer,
        responses={
            200: TripCalendarExportResponseSerializer,
            400: ErrorSerializer,
            404: ErrorSerializer,
            409: TripCalendarBlockedSerializer,
            502: ErrorSerializer,
            503: ErrorSerializer,
        },
    )
    def post(self, request: Request, trip_slug: str) -> Response:
        """Create or refresh this trip's events on the caller's calendar.

        Args:
            request: The authenticated request.
            trip_slug: The trip's URL slug.

        Returns:
            200 with the status block and the number of activity events
            written; 400 when the trip has no dates to place on a calendar;
            404 for a trip this caller is not on; 409 when consent must be
            (re-)established in a browser; 502/503 for upstream and deployment
            failures.
        """
        serializer = TripCalendarExportRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        profile = request.user.profile
        try:
            trip = self.trip(request, trip_slug)
        except TripError as exc:
            return self.error_response(exc)

        account = self._account_or_blocked(request)
        if isinstance(account, Response):
            return account

        trip_url = request.build_absolute_uri(reverse("trips.detail", kwargs={"trip_slug": trip.slug}))
        try:
            # Deliberately not wrapped in transaction.atomic - see the module
            # docstring. One upstream request per event; a transaction here
            # would hold a connection for all of them.
            link, activity_count = export_trip_to_calendar(account, trip, trip_url=trip_url)
        except ValueError as exc:
            # trip_to_event_body's "no dates" refusal. TripError is also a
            # ValueError, but the trip was resolved before this block, so
            # nothing raised in here can be one.
            return Response({"error": str(exc)}, status=400)  # lgtm[py/stack-trace-exposure]
        except (GoogleAuthExpiredError, CalendarNotConfiguredError, GatewayRequestError) as exc:
            return self._gateway_failure(request, account, exc)

        auto_sync = serializer.validated_data.get("auto_sync")
        if auto_sync is not None and link.auto_sync != auto_sync:
            TripCalendarLink.objects.set_auto_sync(link.pk, auto_sync)

        return self._status_response(trip, profile, {"activities_exported": activity_count})

    @extend_schema(
        responses={
            200: TripCalendarRemovalResponseSerializer,
            404: ErrorSerializer,
            409: TripCalendarBlockedSerializer,
            502: ErrorSerializer,
            503: ErrorSerializer,
        },
    )
    def delete(self, request: Request, trip_slug: str) -> Response:
        """Remove this trip's events from the caller's calendar.

        Args:
            request: The authenticated request.
            trip_slug: The trip's URL slug.

        Returns:
            200 with the status block and whether anything was actually
            removed; 404 for a trip this caller is not on; 409 when no calendar
            is connected; 502/503 for upstream and deployment failures.
        """
        profile = request.user.profile
        try:
            trip = self.trip(request, trip_slug)
        except TripError as exc:
            return self.error_response(exc)

        account = self._account_or_blocked(request)
        if isinstance(account, Response):
            return account

        try:
            removed = remove_trip_from_calendar(account, trip)
        except (GoogleAuthExpiredError, CalendarNotConfiguredError, GatewayRequestError) as exc:
            return self._gateway_failure(request, account, exc)

        return self._status_response(trip, profile, {"removed": removed})
