# Tooling docs vs. code: audit

Generated 2026-08-25, by reading the actual scripts, CI config, and test helpers for every
concrete claim in `docs/TOOLING.md` - a different surface again from the other three audit docs:
this round checks whether the doc describing the project's *diagnostic and CI tooling* still
matches what the tooling actually does, not app behavior or test-suite mechanics.

**How to read status:** `MATCHES` = the code does exactly what the doc says, today. `PARTIAL` =
mostly true with a real caveat worth knowing. `STALE`/`CONTRADICTS` would mean the doc is wrong -
neither occurred this round.

## Result

Every claim checked out. Two lightweight verification agents (not a full audit round - `docs/TOOLING.md`
is unusually dense with falsifiable, mechanical claims: file paths, CLI flags, CI wiring, dependency
lists) covered all three sections:

| Section | Claims checked | Status |
| --- | --- | --- |
| Running tests (5 `bin/*` scripts + all documented flags) | 21 | All `MATCHES` |
| Finding where to look (`report_model_writers.py`, `report_defect_history.py`) | narrative only, not independently re-verified this round (already covered by their own cited defect-history entries) | not re-checked |
| Structural checks (CI) - the 5-checker table | 5 checkers + 2 nuance claims | All `MATCHES` |
| Test helpers (`QueryScalingMixin`, `run_concurrently`, `assert_agrees`, `django_perf_rec`) | 4 | All `MATCHES` |
| Evaluated, not adopted (6 rejected tools + their rejection rationale) | 6 | All `MATCHES` |

**The highest-stakes claim in this doc** - that all five structural checkers
(`check_imports_tracked.py`, `check_migration_graph.py`, `check_doc_line_refs.py`,
`check_outage_not_cached.py`, `check_notification_choke_point.py`) are actually wired into CI, not
just present as scripts nobody runs - is true. All five are invoked unconditionally in
`.github/workflows/ci.yml`'s `python-quality` job (lines 96, 103, 110, 118, 121), none gated behind
`continue-on-error` or an `if:`.

## Two informational nuances (not doc errors, not open questions - just worth recording)

1. **`bin/run_integration_tests.sh` "refuses to start against production"** - true as worded, but
   the refusal isn't implemented in the bash wrapper itself; it lives one layer down in
   `tests/integration/lib/env.ts` (`UL_E2E_PRODUCTION_HOSTS` vs. `UL_E2E_ALLOW_PRODUCTION`, covered
   by `docs/TEST_INFRA_DOCS_AUDIT.md`'s own production-guard section). The doc says "the config
   refuses," not "the script refuses" - accurate, just easy to misread as bash-level enforcement.
2. **`notify-bypass-ok:` marker** (`check_notification_choke_point.py`) - the escape-hatch
   mechanism is real and correctly implemented, but has zero current call sites using it (confirmed
   by grep, and independently noted in `docs/FEATURES_CODE_AUDIT.md:71`). Untested in anger, not
   broken.
3. **Pre-commit vs. CI parity** - `.pre-commit-config.yaml` only wires 3 of the 5 structural
   checkers (`imports_tracked`, `outage_not_cached`, `notification_choke_point`); `check_migration_graph.py`
   and `check_doc_line_refs.py` run in CI only. `docs/TOOLING.md` doesn't claim pre-commit parity, so
   this isn't a doc error - noting it here since it's the kind of thing that's easy to assume.

## Fixes applied

None. No code or doc changes were needed.

## Open questions for Jess

None from this round.

## Not yet audited

- `bin/report_model_writers.py` and `bin/report_defect_history.py`'s specific claimed findings
  (e.g. "found four of the five lost updates," "the fix-density half was swept on 2026-08-20 ...
  10 findings, 9 survived, 4 fixed") - these are historical claims tied to `docs/PROBLEMS.md` and
  `docs/reports/2026-08-11-codebase-audit.md`, not currently-true-or-false code claims, so they
  weren't in scope for this pass.
- Whether `docs/reports/2026-08-11-codebase-audit.md`'s Coverage index is itself still accurate.
