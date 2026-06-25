"""Shared Hypothesis strategies for UrbanLens property-based tests.

Import these instead of re-declaring primitives in each test module.
"""
from __future__ import annotations

import decimal
from datetime import date, datetime, timezone

from hypothesis import strategies as st

from urbanlens.dashboard.models.abstract.choices import SecurityLevel
from urbanlens.dashboard.models.friendship.meta import FriendshipStatus, FriendshipType, Permission
from urbanlens.dashboard.models.pin.model import PinType
from urbanlens.dashboard.models.profile.model import MapCenterMode

# ── Safe text ──────────────────────────────────────────────────────────────────
# Restrict to printable ASCII to avoid encoding edge-cases in DB text columns.
# max_codepoint=127 enforces the ASCII boundary; without it, Unicode categories
# like Lu/Ll include non-ASCII characters (e.g. İ, ß) whose Python case-folding
# diverges from PostgreSQL ILIKE, causing spurious test failures.
_printable_alphabet = st.characters(max_codepoint=127, whitelist_categories=("Lu", "Ll", "Nd", "Zs", "Po"))

short_text = st.text(alphabet=_printable_alphabet, min_size=1, max_size=255)
short_text_or_none = st.one_of(st.none(), short_text)
long_text = st.text(alphabet=_printable_alphabet, min_size=0, max_size=2000)

# Non-empty text that is also non-whitespace (suitable for pin nicknames / names).
nonempty_name = st.text(alphabet=_printable_alphabet, min_size=1, max_size=255).filter(str.strip)

# ── Geographic coordinates ─────────────────────────────────────────────────────
# Pin.latitude/longitude are DecimalField(max_digits=9, decimal_places=6).
# Keep values inside valid geographic ranges.

latitude = st.decimals(
    min_value=decimal.Decimal("-90"),
    max_value=decimal.Decimal("90"),
    places=6,
    allow_nan=False,
    allow_infinity=False,
)

longitude = st.decimals(
    min_value=decimal.Decimal("-180"),
    max_value=decimal.Decimal("180"),
    places=6,
    allow_nan=False,
    allow_infinity=False,
)

coord_pair = st.tuples(latitude, longitude)

# Floats for PostGIS-level tests (PinManager.get_nearby_or_create uses floats).
lat_float = st.floats(min_value=-89.9, max_value=89.9, allow_nan=False, allow_infinity=False)
lon_float = st.floats(min_value=-179.9, max_value=179.9, allow_nan=False, allow_infinity=False)
coord_pair_float = st.tuples(lat_float, lon_float)

# Two clearly distinct coordinate pairs (> 1 degree apart) - useful for
# get_nearby_or_create tests that need two non-coincident locations.
_far_lat = st.floats(min_value=-85.0, max_value=85.0, allow_nan=False, allow_infinity=False)
_far_lon = st.floats(min_value=-175.0, max_value=175.0, allow_nan=False, allow_infinity=False)

@st.composite
def two_distant_coord_pairs(draw):
    """Draw two (lat, lon) pairs guaranteed to be > 1 degree apart."""
    lat1 = draw(_far_lat)
    lon1 = draw(_far_lon)
    # Offset by at least 2 degrees so PostGIS proximity check (50 m) never fires.
    lat2 = draw(st.floats(min_value=lat1 + 2.0, max_value=min(89.9, lat1 + 10.0), allow_nan=False, allow_infinity=False))
    lon2 = draw(st.floats(min_value=lon1 + 2.0, max_value=min(179.9, lon1 + 10.0), allow_nan=False, allow_infinity=False))
    return (lat1, lon1), (lat2, lon2)

# ── Numeric fields ─────────────────────────────────────────────────────────────
priority = st.integers(min_value=-9999, max_value=9999)
valid_rating = st.integers(min_value=0, max_value=5)
# Ratings outside the validated [0, 5] range.
invalid_rating_low = st.integers(max_value=-1)
invalid_rating_high = st.integers(min_value=6)

# ── Choices ────────────────────────────────────────────────────────────────────
pin_type = st.sampled_from(list(PinType.values))
security_level = st.sampled_from(list(SecurityLevel.values))
friendship_status = st.sampled_from(list(FriendshipStatus.values))
friendship_type = st.sampled_from(list(FriendshipType.values))
permission_choice = st.sampled_from(list(Permission.values))

# ── Dates and datetimes ────────────────────────────────────────────────────────
reasonable_date = st.dates(min_value=date(1900, 1, 1), max_value=date(2100, 12, 31))
reasonable_datetime = st.datetimes(
    min_value=datetime(1900, 1, 1),
    max_value=datetime(2100, 12, 31),
    timezones=st.just(timezone.utc),
)

# Ordered pair (start ≤ end).
date_range = st.tuples(reasonable_date, reasonable_date).map(
    lambda pair: (min(pair), max(pair))
)

# ── Colour hex strings ─────────────────────────────────────────────────────────
_hex_digit = st.sampled_from("0123456789ABCDEF")
hex_color = st.builds(
    lambda digits: "#" + "".join(digits),
    st.lists(_hex_digit, min_size=6, max_size=6),
)
hex_color_or_none = st.one_of(st.none(), hex_color)

# ── Map center ─────────────────────────────────────────────────────────────────
map_center_mode = st.sampled_from(list(MapCenterMode.values))
# Zoom levels accepted by MapCenterForm (1-19, matching Leaflet's maxZoom).
valid_zoom = st.integers(min_value=1, max_value=19)

# ── Misc ───────────────────────────────────────────────────────────────────────
invalid_security_level = short_text.filter(lambda s: s.lower() not in SecurityLevel.values)
