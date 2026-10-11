"""The boxes REData shards its Overture sync by, so UrbanLens knows where REData's mirror holds anything.

A copy of REData's ``parcels.services.overture.shards.US_STATE_BBOXES`` at release/0.3.7 c4e0de94. REData syncs
each theme by these boxes, padded beyond each state and territory, and serves Overture only where its own
``is_usa_coordinates`` also holds. Alaska's box stops at -179.9, so the western Aleutians are in no shard.

``OvertureShardTableTests`` holds this table equal to the copy a maintainer vendors from a REData checkout.
"""

from __future__ import annotations

#: (min_lon, min_lat, max_lon, max_lat) per state or territory FIPS code.
REDATA_OVERTURE_SHARD_BBOXES: dict[str, tuple[float, float, float, float]] = {
    "01": (-89.0, 29.6, -84.4, 35.5),  # Alabama
    "02": (-179.9, 50.5, -129.0, 71.8),  # Alaska
    "04": (-115.4, 30.8, -108.5, 37.5),  # Arizona
    "05": (-95.1, 32.5, -89.1, 37.0),  # Arkansas
    "06": (-125.0, 32.0, -113.6, 42.5),  # California
    "08": (-109.6, 36.5, -101.5, 41.5),  # Colorado
    "09": (-74.2, 40.5, -71.3, 42.5),  # Connecticut
    "10": (-76.3, 37.95, -74.5, 40.35),  # Delaware
    "11": (-77.6, 38.3, -76.4, 39.5),  # District of Columbia
    "12": (-88.1, 23.9, -79.5, 31.5),  # Florida
    "13": (-86.1, 29.9, -80.3, 35.5),  # Georgia
    "15": (-160.8, 18.4, -154.3, 22.75),  # Hawaii
    "16": (-117.7, 41.5, -110.5, 49.5),  # Idaho
    "17": (-92.0, 36.5, -86.5, 43.0),  # Illinois
    "18": (-88.6, 37.3, -84.3, 42.3),  # Indiana
    "19": (-97.1, 39.9, -89.6, 44.0),  # Iowa
    "20": (-102.5, 36.5, -94.1, 40.5),  # Kansas
    "21": (-90.1, 36.0, -81.5, 39.6),  # Kentucky
    "22": (-94.5, 28.4, -88.3, 33.5),  # Louisiana
    "23": (-71.6, 42.6, -66.5, 47.9),  # Maine
    "24": (-80.0, 37.4, -74.6, 40.2),  # Maryland
    "25": (-74.0, 40.7, -69.4, 43.4),  # Massachusetts
    "26": (-90.9, 41.2, -81.6, 48.8),  # Michigan
    "27": (-97.7, 43.0, -89.0, 49.9),  # Minnesota
    "28": (-92.1, 29.7, -87.6, 35.5),  # Mississippi
    "29": (-96.3, 35.5, -88.6, 41.1),  # Missouri
    "30": (-116.5, 43.9, -103.5, 49.5),  # Montana
    "31": (-104.5, 39.5, -94.8, 43.5),  # Nebraska
    "32": (-120.5, 34.5, -113.5, 42.5),  # Nevada
    "33": (-73.1, 42.2, -70.1, 45.8),  # New Hampshire
    "34": (-76.1, 38.4, -73.4, 41.9),  # New Jersey
    "35": (-109.5, 30.8, -102.5, 37.5),  # New Mexico
    "36": (-80.3, 40.0, -71.3, 45.5),  # New York
    "37": (-84.8, 33.3, -75.0, 37.1),  # North Carolina
    "38": (-104.5, 45.4, -96.0, 49.5),  # North Dakota
    "39": (-85.3, 37.9, -80.0, 42.8),  # Ohio
    "40": (-103.5, 33.1, -93.9, 37.5),  # Oklahoma
    "41": (-125.0, 41.5, -116.0, 46.8),  # Oregon
    "42": (-81.0, 39.2, -74.2, 42.8),  # Pennsylvania
    "44": (-72.4, 40.6, -70.6, 42.5),  # Rhode Island
    "45": (-83.9, 31.5, -78.0, 35.7),  # South Carolina
    "46": (-104.5, 42.0, -95.9, 46.5),  # South Dakota
    "47": (-90.8, 34.5, -81.1, 37.2),  # Tennessee
    "48": (-107.2, 25.3, -93.0, 37.0),  # Texas
    "49": (-114.5, 36.5, -108.5, 42.5),  # Utah
    "50": (-74.0, 42.2, -70.9, 45.5),  # Vermont
    "51": (-84.2, 36.0, -74.7, 40.0),  # Virginia
    "53": (-125.3, 45.0, -116.4, 49.5),  # Washington
    "54": (-83.1, 36.7, -77.2, 41.1),  # West Virginia
    "55": (-93.4, 42.0, -86.3, 47.6),  # Wisconsin
    "56": (-111.5, 40.5, -103.5, 45.5),  # Wyoming
    "60": (-171.6, -14.9, -167.6, -10.5),  # American Samoa
    "66": (144.1, 12.7, 145.5, 14.2),  # Guam
    "69": (144.4, 13.6, 146.6, 21.1),  # Northern Mariana Islands
    "72": (-68.4, 17.4, -64.7, 19.0),  # Puerto Rico
    "78": (-65.5, 17.1, -64.1, 18.9),  # US Virgin Islands
}


def in_redata_overture_shard(latitude: float, longitude: float) -> bool:
    """Whether any of REData's Overture shard boxes contains a coordinate, edges included.

    Args:
        latitude: WGS-84 latitude.
        longitude: WGS-84 longitude.

    Returns:
        True when some shard covers the point.
    """
    return any(min_lon <= longitude <= max_lon and min_lat <= latitude <= max_lat for min_lon, min_lat, max_lon, max_lat in REDATA_OVERTURE_SHARD_BBOXES.values())
