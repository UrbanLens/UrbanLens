---
status: accepted
date: 2026-09-01
---

# A concealed viewer sees a wiki identical to a place with no contributions

Formerly `D2`. Detail: [`docs/designs/concealed-wiki-spec.md`](../designs/concealed-wiki-spec.md).

Viewers flagged as concealed (behaviour suggesting data-mining) can still reach wikis they earned, but must not learn anything the community contributed there. For such a viewer the wiki page, every partial, JSON endpoint and external-API route that resolves through the wiki must be byte-equivalent to the same place with zero user contributions and the same enrichment history. The rules are Jess's: hide user-contributed content, show automatically fetched content, always unset security indicators, always hide markup.

## Consequences

- Most of the work is recomputing derived values (counts, orderings, timestamps) over the visible set, not hiding fields: of 310 classified items, 135 were derived.
- Over-concealing is also a tell; automatic content must still show.
- A concealed wiki renders; it never 404s where the place plainly exists.
- Concealment and access are independent: concealment never grants access, and access never disables concealment. It is applied through a read-only presentation proxy and the existing per-relation chokepoints, never by mutating rows.
