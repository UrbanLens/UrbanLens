# D19 — Engaging with a Place's wiki grants permanent access

> **Written by a Claude agent. Not authoritative.**
>
> This records what one automated session measured or believed on the date
> below. It was not independently reviewed, its numbers may be stale, and the
> code may have moved. Re-run the measurement before relying on it, and
> **rewrite this file** when you do — do not add a correction underneath the
> old claim. When this file and the code disagree, the code wins.

`id: D19` · `status: accepted` · `updated: 2026-09-23` · `ruled by: Jess, 2026-09-23` · `amends: D1 (GOALS.md "Wiki access"), D4 (PlaceAccessGrant written only by splits)`

## Decision

A profile keeps access to a wiki if it views the wiki, or shares content to it, while it holds
access and the wiki's Location resolves to a Place. The access survives even after every qualifying
pin is later moved or deleted. The grant covers the Place's whole access domain, the same unit any
pin inside it earns.

A placeless Location (access by exact pin match, circle-fallback boundary) has no domain to grant.
There, access still ends with the pin.

## Rationale

A contributor should not be locked out of a wiki they contributed to because they later moved or
deleted a pin. Before this ruling the behaviour already existed and had been confirmed once, when
P88 was closed on 2026-09-08 (`docs/archive/PROBLEMS-ARCHIVE.md`, "every wiki view minted a
permanent access grant"). No D record held it, and N27 listed it as an open decision the suite
avoided asserting. This record closes that.

The grant only entrenches access the profile already held legitimately. Nothing is granted on a
refused request: the grant is recorded after `location_visible_to` passes, so a profile that never
had access cannot earn it by probing.

## Where it lives

- `PlaceAccessGrantManager.record_engagement` — `src/urbanlens/dashboard/models/place/queryset.py:163`.
  A `get_or_create` under `GrantReason.GRANDFATHERED_ENGAGEMENT`
  (`models/place/model.py:55`). A no-op for a missing profile or a None place.
- Viewing: `resolve_visible_wiki` — `src/urbanlens/dashboard/services/wiki/wiki_access.py:390`,
  after the `location_visible_to` 404. Nearly every wiki-scoped web controller and external API
  handler resolves through it, so a request the gate admits records the grant, whether it reads or
  writes. The exception is `LocationDetailPinJsonView` (`controllers/detail_pins.py:295`), which
  checks `location_visible_to` itself and records nothing. Every surface honours a grant once it
  exists, because the grant is read inside `location_visible_to`.
- Sharing: `WikiShareService.share_from_pin` — `src/urbanlens/dashboard/services/wiki/wiki_share.py:110`,
  gated on `shared`. Opening the share dialog (`controllers/wiki_share.py`, GET), or posting it with
  nothing selected, grants nothing.
- Read back through `PlaceAccessGrantManager.granted_domain_ids`, which
  `wiki_access.accessible_domain_ids` unions into the domain set (`wiki_access.py:140`).

Tests: `src/urbanlens/dashboard/tests/hypothesis/test_grandfathered_parcel_split_access.py`:
`WikiEngagementGrandfatheringTests`, `WikiEngagementAcrossSurfacesTests` (11 web routes and 8
external API routes answer the same after the pin is gone; deleting the grant row brings back the
404; a stranger and a lapsed non-engager get 404 everywhere and no row) and
`PlacelessWikiEngagementTests`. The Playwright spec
`tests/integration/specs/security/wiki-access.spec.ts` asserts only the placeless half, because a
pin gets a Place only from a paid REData lookup, which only the opt-in `location` project makes.

## What this widens

- **`docs/GOALS.md`, "Wiki access"** says a pin inside the boundary earns access and "nothing else
  grants access". Engagement is now a second route to *keeping* access, but not to *getting* it.
  GOALS.md is Jess's to amend. This record does not change it.
- **D4** (`docs/designs/place-consolidation.md:138`) says `PlaceAccessGrant` is written "only by
  split processing". The engagement grant is a second writer, and migration `0027_places_backfill`
  wrote `GRANDFATHERED_BACKFILL` rows once. D4 stays accepted for everything else it decides.

Not measured this session: how many engagement grants exist on staging or production.
