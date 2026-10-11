---
status: accepted
date: 2026-09-15
---

# The site is built for 1,000 concurrent signed-in users now and 10,000 later

Formerly `D15`.

The targets are Jess's: 1,000 concurrent users now and 10,000 eventually. A concurrent user is defined as a person browsing with a log-normal think time (30 s median) between weighted page views, plus the polls and notification socket an open page keeps up. "Supports" means a full page at p95 under 1 s, a fragment or JSON call at p95 under 500 ms, under 0.5% failed requests, and the Postgres pool never above 80%. The load model and budgets are an agent's proposal, to be replaced with real traffic when there is some.

## Consequences

- This is separate from ADR-0011: a site can isolate users and still fall over at 200, or carry 1,000 while one import stalls everyone.
- The model has no writes yet, so a figure from it is a ceiling on reading. It runs on chiron, not the production host.
