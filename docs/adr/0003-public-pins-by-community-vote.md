---
status: accepted
date: 2026-08-27
---

# Public locations are chosen by a strict community vote, one per region

Formerly `D3`.

New accounts need something on their map, but a pin must never become visible without earned access unless it is safe and well known. A location becomes a vote candidate only when every rule holds: no public location within 15 km, low community-rated vulnerability, a complete wiki, a minimum number of distinct pinners, and a top-10 rank in its state. Users who have a pin there vote; the vote passes after at least 7 days with 2 or more votes and at least 75% yes, and fails permanently at 10 or more votes with at least 75% no. The product goals set the bar deliberately high, so this is expected to trigger rarely.

## Consequences

- A passed location is public permanently; only an admin or a data migration reverts it. It is offered to other users as a pin suggestion, which they can turn off.
- The UI never explains the rules; ineligible or suspended candidates render nothing.
- All thresholds live in `PublicPinConfig`. Built 2026-07-23, although the source doc is still marked DRAFT.
