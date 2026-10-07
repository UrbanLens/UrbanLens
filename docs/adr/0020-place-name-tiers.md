---
status: accepted
date: 2026-09-23
---

# A place's automatic name is ranked by kind of name before source

Formerly `D20`. Detail: [`docs/designs/place-name-tiers.md`](../designs/place-name-tiers.md).

A staging pin on the Hudson River State Hospital campus was titled after the private road under it, because Nominatim came first in the source priority and nothing told a road from a place. Every automatic name candidate now carries a tier, which decides before the admin's source priority: Wikipedia article, then a historic-register listing whose own boundary contains the point, then site, building, point of interest, and road. Jess asked for the explicit ranking and ruled that the Wikipedia article beats the register listing, since a listing can name a single building on a larger property.

## Consequences

- A road or address never names a place, and one already in place is retired.
- A building names a property only when the property is known to hold exactly that one building.
- An automatic name gives way to a strictly better tier; a name a person wrote never does.
- A register listing must contain the point; mere presence in a nearby result is not enough.
