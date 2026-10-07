---
status: accepted
date: 2026-10-07
---

# Calendar writes follow visibility changes, but only to calendars set to auto-sync

Decided by Jess for UrbanLens#301 ("hidden location stays on an unreached calendar").

On a trip with auto-sync, any change to what a member may see queues the push an edit would, through the existing push sweep and the `google_calendar` rate limiter. A deleted stop or trip has the events UrbanLens made for it deleted (queued, rate-limited, idempotent on 404/410); an event an import linked from the user's own calendar is never deleted. An export without auto-sync is never pushed automatically, since its owner did not ask for updates.

## Consequences

- `manage.py clear_withheld_calendar_locations` cleans what 0.8.0 exports left on calendars nothing pushes to, once, at the 0.9.0 rollout (N46).
- After that, a location hidden after an export without auto-sync stays on that calendar until its owner exports again.
- "Never deleted" holds on every path, the user's own "remove from calendar" included: an imported event is only unlinked (UrbanLens#330).
- The command rewrites an imported event only when its fingerprint shows UrbanLens wrote what is now withheld (UrbanLens#333). Writes from before 0.9.0 have no fingerprint, so a location 0.8.0 wrote onto a user's own event cannot be told from the user's and stays; the command counts those.
