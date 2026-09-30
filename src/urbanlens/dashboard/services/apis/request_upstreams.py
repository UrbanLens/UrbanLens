"""Every upstream a view calls while the user waits, with its deadline and per-account rate.

Kept in one module so the budgets can be read and compared side by side. Each class has its own slots
(see :class:`~urbanlens.dashboard.services.core.request_upstream.RequestUpstream`).
"""

from __future__ import annotations

from urbanlens.dashboard.services.core.request_upstream import RequestUpstream
from urbanlens.dashboard.services.security.throttle import Rate


class PlacesAutocompleteUpstream(RequestUpstream):
    """Place predictions as the user types in the map search."""

    name = "places.autocomplete"
    deadline = 5.0
    rate = Rate(limit=120, window_seconds=60)


class PlaceResolveUpstream(RequestUpstream):
    """Coordinates for the one suggestion the user picked."""

    name = "places.resolve"
    deadline = 8.0
    rate = Rate(limit=60, window_seconds=60)


class PlaceDetailsUpstream(RequestUpstream):
    """The Places layer's detail card for one place."""

    name = "places.details"
    deadline = 8.0
    rate = Rate(limit=60, window_seconds=60)


class NearbyLandmarksUpstream(RequestUpstream):
    """Historical landmarks for the Places layer, from REData or Google."""

    name = "places.landmarks"
    deadline = 8.0
    rate = Rate(limit=60, window_seconds=60)


class NationalParksUpstream(RequestUpstream):
    """National parks near a map click, from REData's catalogue."""

    name = "places.parks"
    deadline = 8.0
    rate = Rate(limit=60, window_seconds=60)


class WikipediaNearbyUpstream(RequestUpstream):
    """Geotagged Wikipedia articles near a map click."""

    name = "places.wikipedia"
    deadline = 8.0
    rate = Rate(limit=60, window_seconds=60)


class WeatherForecastUpstream(RequestUpstream):
    """Forecasts for a trip's upcoming activities."""

    name = "weather.forecast"
    deadline = 8.0
    rate = Rate(limit=30, window_seconds=60)


class HistoricalMapsBrowseUpstream(RequestUpstream):
    """The list of georeferenced sheets covering a pin or wiki."""

    name = "historical_maps.browse"
    deadline = 10.0
    rate = Rate(limit=30, window_seconds=60)


class RedataMediaUpstream(RequestUpstream):
    """One file proxied from REData. Throttled on its routes rather than here, since they are anonymous."""

    name = "redata.media"
    deadline = 30.0


class FlickrAlbumUpstream(RequestUpstream):
    """A pasted public Flickr album, resolved for the preview grid."""

    name = "flickr.album"
    deadline = 20.0
    rate = Rate(limit=20, window_seconds=60)


class RemoteImageCopyUpstream(RequestUpstream):
    """The first download of a third-party image this site keeps a copy of (``services.media.remote_copies``).

    A new gallery asks for every tile at once; a request over the slots answers "busy" and the page retries.
    """

    name = "media.remote_copy"
    deadline = 25.0
    rate = Rate(limit=600, window_seconds=60)


class RemoteTileUpstream(RequestUpstream):
    """The first download of a foreign map tile this site keeps (``services.map.remote_tiles``).

    A map asks for a whole viewport at once; a request over the slots answers "busy" and the tile layer retries.
    """

    name = "map.remote_tiles"
    deadline = 15.0
    rate = Rate(limit=1200, window_seconds=60)
