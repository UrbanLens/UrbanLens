---
status: accepted
date: 2026-10-07
---

# Production spends the shared external budgets; staging gets a sliver, development none

Formerly `D26`.

Every UrbanLens host shares production's network address and API accounts, so development and staging calls spend production's budgets. A 2026-10-05 audit found development making 13,291 calls in a week. Jess ruled that production consumes the bulk of every budget, staging makes few requests, and development makes almost none (amended 2026-10-06: development never calls hosted AI providers). Every external service is classified, and one policy decides per environment where every call passes. REData and internal services are always allowed. Quota and billed services get `UL_ENVIRONMENT_SHARE` of each budget (production 0.9, staging 0.05, development 0). Hosted AI runs on production and staging only. Messaging goes to the console and third-party writes are refused off production. Off production, only internal beat entries are scheduled. The rule is Jess's; the mechanism and classification are an agent's.

## Consequences

- A refusal raises `EnvironmentRefusedError`, a non-transient `ServiceDisabledError`, so callers that already degrade on a disabled service keep working. Nothing caches it as an empty answer.
- An unset `UL_ENVIRONMENT` refuses to start (Jess, 2026-10-07).
- The share is per deployment: more than one staging deployment needs `UL_ENVIRONMENT_SHARE=0` or a smaller share, or the total passes the budget.
- `UL_ALLOW_OUTBOUND_APIS` and `UL_BILLED_API_SHARE` are retired. An unclassified service is treated as billed, and a test fails on any service left unclassified.
