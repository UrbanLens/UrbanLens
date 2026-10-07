---
status: accepted
date: 2026-10-07
---

# A calendar connection is dropped only when Google refuses the user's grant

Decided by Jess for UrbanLens#302 ("a failed token refresh drops the calendar connection").

Only Google's explicit refusal of the grant deletes a `GoogleCalendarAccount`: `invalid_grant` or `admin_policy_enforced` from the token endpoint, a 401 that one fresh access token does not cure, or a 403 naming a reason that is not a rate limit, not about one event and not about the site. A failure that passes (a 5xx, 408 or 429, no answer, a rate-limit reason) answers "busy" and leaves the write owed for the push sweep. A refusal of the site (its Google project, its OAuth client, or a 403 naming no reason) is logged at ERROR and reported as calendar sync being unavailable. Reconnecting costs the user a consent screen, and a grant Google did revoke fails again on the next call.

## Consequences

- A write held up by a failure that passes does not count toward `MAX_CALENDAR_PUSH_ATTEMPTS`; `MAX_OWED_CALENDAR_WRITE_AGE` (30 days) bounds it instead, so a long outage keeps the sweep retrying.
