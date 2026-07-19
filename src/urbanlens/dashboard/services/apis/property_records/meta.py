"""Shared constants and error types for the property-records package."""

SCRAPE_USER_AGENT = "Mozilla/5.0 (X11; CrOS x86_64 14541.0.0) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36"


class SourceUnreachableError(Exception):
    """A property-record source couldn't be reached (transport failure, 5xx, or rate-limit exhaustion).

    Deliberately distinct from an empty result: "the county server is down
    right now" must never be recorded as "this parcel has no data" - the
    former is transient and worth retrying soon, the latter is a cacheable
    fact about the parcel. The orchestrator maps this to
    ``REASON_SOURCE_ERROR``, which callers must not negative-cache
    (see ``plugins.builtin.property_records._fetch_payload``).
    """
