# Location-data docs vs. code: audit

Generated 2026-08-25, by reading the actual code and TypeScript specs for every concrete claim in
`docs/LOCATION_DATA_TESTS.md` - the opt-in Hudson River State Hospital suite, and the sixth and
(for now) final surface in this series, after `docs/audits/GOALS_CODE_AUDIT.md`, `docs/audits/FEATURES_CODE_AUDIT.md`,
`docs/audits/TEST_INFRA_DOCS_AUDIT.md` and `docs/archive/TOOLING_AUDIT.md`. This doc is different in kind from the
other four: most of it is deliberately self-aware methodology (bounds not values, questions not
verdicts), not simple factual claims - so the audit targeted the parts that *are* falsifiable: the
three-defect boundary-invention fix it documents as already fixed, the fixture/spec structure it
describes, two "vacuous assertion" caveats, and its project-config/numeric claims. The live specs
themselves were not run (real money, real time against REData/EPA/Wikipedia/county GIS - out of
scope for a doc-accuracy pass); this is a static code-vs-doc check, same method as the TOOLING.md
round.

**How to read status:** `MATCHES` = the code does exactly what the doc says, today. `PARTIAL` =
mostly true with a real caveat. `STALE` = the doc was accurate once but the code has since changed.
`CONTRADICTS` = the code does something different from what the doc claims, today.

## Summary

| Topic | Status |
| --- | --- |
| Boundary-invention fix (3 compounding defects) still in code | `MATCHES` |
| Fixture/spec structure claims (`fixtures.ts`, `requireBoundary()`, `waitFor`, `panel_fetch`, redata.py failure modes) | `MATCHES` |
| Vacuous-assertion caveats (pinned-user count, draft-wiki invisibility) | `PARTIAL` |
| Project-config/ordering claims (single worker, DB constraint, sale ordering, parcel-area bounds) | `PARTIAL` |

## Boundary-invention fix - `MATCHES`

The three compounding defects the doc describes as fixed on 2026-08-24/25 - `resolve_location_place`
over-stamping `place_resolved_at`, the DB write not mirrored to the in-memory instance, and
`resolve_for_pin` returning the pin's own invented hull ahead of the real place polygon - are
genuinely fixed in the current codebase. `git log` shows commit `3eafd395` ("fix: prefer a
provider's parcel over a boundary we invented", 2026-08-24 23:05:38) naming exactly these three
fixes, and `git diff 3eafd395 HEAD -- resolution.py boundaries.py queryset.py` is empty - nothing
since has touched or reverted it. No action needed.

## Fixture/spec structure claims - `MATCHES`

`fixtures.ts` genuinely never throws on a missing-geometry case (it stores a diagnosis instead);
`hrsh-boundary.spec.ts` really is the one spec that reports missing geometry as a failure rather
than skipping via `requireBoundary()`; `lib/waiting.ts`'s `waitFor` really does produce a
what/how-many-times/what-it-last-saw diagnostic on timeout; `panel_fetch` really is a separately
queued Celery worker (`docker-compose.yml`'s `celery-worker-panels`, distinct from the default
`celery-worker`) - a real deployment trap, not documentation flourish; and `redata.py`'s docstring
supports both named failure modes (a too-small building-derived hull, the oversized CRIS zone) in
substance.

One trivial doc omission, not a contradiction: `hrsh-pin-enrichment.spec.ts` does not call
`requireBoundary()` either - deliberately, since its assertions key off the coordinate rather than
the parcel. The doc's "everything else skips with a pointer to it" framing is still true of every
spec that actually depends on the parcel; this one just doesn't. Not worth a doc edit on its own.

## Vacuous-assertion caveats - `PARTIAL`

The draft-wiki claim ("created automatically in a sense nobody can observe" - `officially_created=False`,
every visible surface treats that as no wiki) is fully accurate: 8 independent call sites across
controllers, search, autocomplete, achievements and safety code all gate on `officially_created`,
and the one internal read path that can see a draft is never exposed.

The pinned-user-count claim is split. "The template hardcodes 3 while `MIN_VISIBLE_PIN_COUNT` is
Python-only" is still exactly true. But "`wiki_community_summary` counts pins on one `Location`
row" was true as of `HEAD` (commit `86e55aa1`) and is **not** true of the working tree right now -
there's an uncommitted, in-progress change to `community_counts.py` that makes the count
Place-aware (aggregating across every `Location` sharing the wiki's `Place`), which is precisely
the fix that would close the gap this doc describes. The change ships with its own uncommitted
test class and cites `docs/audits/GOALS_CODE_AUDIT.md` as its source - almost certainly another concurrent
agent session working one of that doc's open items, per the project's own note that multiple agents
edit this repo at once. Not touched, and the doc's claim was left as-is (it's accurate against the
last commit, which is the right reference point for documentation) rather than edited to describe
work that hasn't landed. Worth a fresh look once that change is committed.

## Project-config/ordering claims - `PARTIAL`

Single-worker config (`workers: 1` on the `location` Playwright project) and the DB constraint it
depends on (`db_pin_unique_location_per_profile`, a real migrated `UniqueConstraint`) both match.
`WikiPropertySale`'s inherited ordering (`["-sale_date", "-created"]`) matches exactly. All three
illustrative "invariants of the application" examples trace to real spec assertions.

The parcel-area bounds were stale: the doc said 200,000-2,000,000 m², the code asserts
50,000-1,500,000 m². This wasn't drift so much as a documented bugfix the doc missed - an earlier
fixture version used the same "~156 acres" figure the doc quotes and set a 200,000 m² floor, which
rejected the real parcel once REData's live measurement (133,964 m²) came in under it. **Fixed**:
`docs/LOCATION_DATA_TESTS.md`'s bounds section rewritten to the current numbers and the reason they
moved.

## Fixes applied (2026-08-25)

1. `docs/LOCATION_DATA_TESTS.md` - parcel-area bounds corrected from the stale 200,000-2,000,000 m²
   to the code's actual 50,000-1,500,000 m², with the live-measurement rationale for why it moved.

## Open questions for Jess

None. The one real gap found (pinned-user-count vacuity) already has a fix in flight from another
session - nothing here needs a decision from you.

## Not yet audited

- The live specs were never executed against a real deployment as part of this pass (out of scope,
  costs real money/time) - this audit only confirms the code matches what the doc *says* the specs
  do, not that a live run currently passes.
- `docs/PROBLEMS.md`'s full write-up of the three-defect fix wasn't independently re-verified beyond
  the git-history corroboration above.
