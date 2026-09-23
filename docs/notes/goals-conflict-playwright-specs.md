# N27 — Eight GOALS.md conflicts are now codified as expected Playwright failures, not left implicit

> **Written by a Claude agent. Not authoritative.**
>
> This records what one automated session measured or believed on the date
> below. It was not independently reviewed, its numbers may be stale, and the
> code may have moved. Re-run the measurement before relying on it, and
> **rewrite this file** when you do — do not add a correction underneath the
> old claim. When this file and the code disagree, the code wins.

`id: N27` · `type: note` · `status: current` · `updated: 2026-09-23` · `source: tests/integration/specs/{security,api,ui,slow}/*.spec.ts, this checkout`

Where a spec asserts a `docs/GOALS.md` rule the product does not yet meet, it is written as a
`test.fail()` carrying a `type: "goals-conflict"` annotation (`test.info().annotations.push({...})`)
rather than skipped or left uncovered. The spec still runs and still turns the suite red - as an
**unexpected pass** - the day the product meets the rule, so the fix is caught rather than silently
absorbed into "tests were already green."

Eight conflicts are codified this way, as of this session:

| Rule (GOALS.md) | Spec | What it actually does today |
| --- | --- | --- |
| A pin share is a suggestion, never a live reference | `tests/integration/specs/security/pin-share.spec.ts:119-127` | A pending share reads through to the sender's *live* pin until accepted (`services/sharing/pin_sharing.py:136-172` reads `share.pin`'s facts at accept time, not a snapshot taken at share time) - a sender's edit made after sharing but before the recipient accepts still lands in the copy |
| A pin share is a suggestion, never a live reference | `pin-share.spec.ts:193-209` (two `test.fail()` blocks, `LIVE_NAME_CONFLICT`) | The share preview shows the sender's *current* pin name, both while pending and after acceptance, not the name at share time - a live reference, not a suggestion |
| Pin data reaches a wiki only as an opt-in, per-field copy | `tests/integration/specs/security/pin-wiki-copy.spec.ts:137-149` | Rating/danger sync from pin to wiki by default, not opt-in |
| Editing a pin must never change another user's view of a copy | `tests/integration/specs/security/trip-litmus.spec.ts:122-144` | Trip activities read the live pin rather than a copy - editing the pin changes the trip |
| DMs are E2EE, unconditionally | `tests/integration/specs/security/dm-e2ee.spec.ts:83-85` | The server accepts a plaintext direct message; nothing rejects an unencrypted payload |
| A manual list edit is the user's decision, not the filter's | `tests/integration/specs/api/smart-lists.spec.ts:81,127,156` | A manual removal from a smart list is resurrected by the saved filter on the next resync, and a manual add is dropped when the pin stops matching - see `src/urbanlens/dashboard/services/pins/pin_list_membership.py:86` (delete path keyed only on `added_via != ADDED_MANUAL`, no tombstone for a manual removal) and `:234` (`add_pin_ids_to_list` skips a pin already present rather than upgrading its `added_via` to `ADDED_MANUAL`, so a manual add of an already-rule-matched pin is indistinguishable from a rule add and gets swept on the next resync) |
| Every map carries the same controls | `tests/integration/specs/ui/map-controls.spec.ts:161-180` | Map pages diverge - e.g. `templates/dashboard/partials/layout/_map_annotations_panels.html:33-43` renders no `{% map_search_bar %}` |
| Self-destruct means deleted | `tests/integration/specs/slow/dm-self-destruct.spec.ts:109-111` | A read self-destructing message is only hidden immediately; the hourly sweep (`:47` past the hour) is what actually hard-deletes it - the immediate-delete check is the `test.fail()`, the sweep check passes |

Ruled on 2026-09-23: "common pins" (pins two friends both hold) are **not** visible to each other by
default - `Profile.common_pins_visibility` defaults to `NO_ONE`, and each side has to opt in.

Ruled on 2026-09-23: an unread self-destructing message times out 180 days after it was sent
(`UNREAD_SELF_DESTRUCT_TIMEOUT`), and the hourly sweep deletes it - the case it covers is a recipient
who has gone inactive.

Ruled on 2026-09-23 (D19): viewing, or sharing to, a Place-resolved wiki while holding access grants
that access **permanently** - it survives the pin being moved or deleted. A placeless location has no
domain to grant, so `wiki-access.spec.ts` keeps asserting the exact-location revoke there; the Place
case is covered in pytest (`test_grandfathered_parcel_split_access.py`), since only the paid
`location` project gives a pin a Place.

## Open decisions, not ruled on by any spec

None as of 2026-09-23.

See `docs/INTEGRATION_TESTS.md` (R7) for how these specs fit into the suite as a whole, and
`docs/PROBLEMS.md` for defects that are not GOALS conflicts.
