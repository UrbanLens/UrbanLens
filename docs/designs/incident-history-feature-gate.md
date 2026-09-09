# D10 — A block's incident history is its own paid flag, not `NEARBY_RESEARCH`

> **Written by a Claude agent. Not authoritative.**
>
> This records what one automated session measured or believed on the date
> below. It was not independently reviewed, its numbers may be stale, and the
> code may have moved. Re-run the measurement before relying on it, and
> **rewrite this file** when you do — do not add a correction underneath the
> old claim. When this file and the code disagree, the code wins.

`id: D10` · `status: accepted` · `updated: 2026-09-08`

Recorded while shipping the paid "Incident History" panel
(`plugins/builtin/redata_incidents.py:89-153`), which needed a
`PanelSource.required_feature` and had two existing candidates to reuse or
avoid: `SiteFeature.NEARBY_RESEARCH` (already gates one other panel) and
"just leave it free like every sibling panel REData's incidents work has
shipped so far."

## Decision 1: a dedicated `SiteFeature.INCIDENT_HISTORY`, not `NEARBY_RESEARCH`

`SiteFeature.NEARBY_RESEARCH` already exists and already gates exactly one
panel - `EpaEchoNearbyPanelSource`, EPA ECHO's nearby-regulated-facilities
list (`models/subscriptions/model.py:41-51`; confirmed by
`test_panel_feature_gate.py`, whose module docstring said so until this
change). Reusing it for Incident History was the cheaper option: no new
enum member, no new admin-role checkbox, one less thing for a site operator
to configure.

Rejected anyway. `NEARBY_RESEARCH`'s own docstring frames it as "nearby
research data" - a bundle of *facility/feature lookups near the pin* (the
newer nearby-data panels named there - Cameras & Structures, Underground
Structures, Permits & Violations, Reported Incidents, Water & Hydrology,
Site Conditions, Fire & Disaster History - were all deliberately left free,
not folded into this flag). A block's multi-year crime-report history is a
different kind of thing to sell than a facility list: it is more sensitive
(a pattern of reported incidents at a specific block, over decades, reads
differently than "there is an EPA-regulated facility nearby"), and a site
operator may reasonably want to price or restrict it independently of the
rest of the nearby-research bundle - it is its own product/pricing lever,
not a variant of one that already exists. Folding it into `NEARBY_RESEARCH`
would force every future decision about *this* data's access policy through
a flag named for a different concept, and would make revoking or re-pricing
it impossible without also touching every other panel the shared flag
covers.

`INCIDENT_HISTORY` costs one enum member
(`models/subscriptions/model.py:58`, with the per-flag comment convention
every other member follows) and one `PanelSource.required_feature`
assignment. `test_panel_feature_gate.py`'s module docstring is corrected to
say two sources now declare `required_feature`, not one.

## Decision 2: the existing free "Reported Incidents" panel stays free

`PoliceIncidentsPanelSource` (`plugins/builtin/redata_incidents.py:37-86`,
the last-3-years/top-6-rows panel) is untouched by this change and
deliberately not gated. Two reasons, not one:

- `NEARBY_RESEARCH`'s own docstring already named "Reported Incidents" as
  one of the panels "deliberately left free" - so gating it now would be
  reversing a decision already made and documented, not applying a new one.
- Gating an existing free panel takes something away from users who already
  have it. That was never the ask here; the brief was a *deeper* pull of
  the same endpoint. `IncidentHistoryPanelSource` is purely additive: same
  gateway call (`RedataIncidentsGateway.get_incidents`), a wider `years`
  window (25 vs. 3, REData's own ceiling), and a year-by-year trend instead
  of a short recent-rows list - a materially deeper view of the same
  underlying data, not a re-skin of it behind a paywall.

## What this does not decide

- **Pricing or which `SubscriptionRole`s grant `INCIDENT_HISTORY`.** That is
  ordinary site-admin configuration (`SubscriptionRole.features`), not part
  of this decision.
- **Whether any other "nearby research" panel should later get its own
  dedicated flag.** This decision is about incident history specifically,
  reasoned from its sensitivity and its independent pricing appeal - not a
  general rule that every panel should eventually get its own flag. Reusing
  `NEARBY_RESEARCH` remains the right default for a panel that does not have
  a distinct reason to be sold separately.

## Verification

Browser-verified against the real dev stack (`urbanlens_development_main_app`):
the gated "Incident History" card appears/disappears from the Private Pin
page based on the `INCIDENT_HISTORY` grant, and the free "Reported
Incidents" card renders unconditionally either way. Not re-measured this
session; taken from the implementing session's report.
