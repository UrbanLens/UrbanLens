---
status: accepted
date: 2026-08-27
---

# Place is the single answer to "is this the same place?"

Formerly `D4`. Detail: [`docs/designs/place-consolidation.md`](../designs/place-consolidation.md).

Pin dedup, wiki access, wiki dedup and "places in common" each answered "same place?" differently, which warped coordinates, duplicated wikis and official geometry, and left access filtering upheld only by discipline. A `Place` (parcel, building, site) now owns the official geometry and anchors the wiki, and every subsystem resolves through it. Dedup is boundary-based with no radius, so exact coordinates are always kept. Access derives only from official geometry; user and community drawings live in a table no access path reads. Each parent edge carries its access meaning: `PART_OF` places form one access domain that a pin anywhere in it grants in both directions, and a `MEMBER_OF` parent is earned only by holding every member.

## Considered options

- Concrete `Parcel`/`Building` subclasses: rejected, because they put a join in the access predicate and make a new kind a schema migration.
- Keying access semantics off `Place.status`: rejected while building it, because it could not express a campus spanning several current tax parcels.

## Consequences

- `PlaceAccessGrant` is written by split processing and the one-time backfill, and since ADR-0019 also by wiki engagement. No API surface writes it.
