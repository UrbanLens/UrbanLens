---
status: accepted
date: 2026-09-08
---

# A block's incident history is gated by its own paid flag

Formerly `D10`.

The paid Incident History panel (a block's multi-year crime-report history) is gated by a dedicated `SiteFeature.INCIDENT_HISTORY`, not by the existing `NEARBY_RESEARCH`. It is more sensitive than a list of nearby facilities, and an operator may want to price or restrict it independently. Folding it into a flag named for another concept would tie every future pricing or revocation change to the other panels that flag covers.

## Consequences

- The free "Reported Incidents" panel (last three years, top six rows) stays free and ungated.
