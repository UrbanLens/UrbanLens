# Reply: `:main` now names only tested code, P181's step is migration 0034, and releases publish again

- **Status: SENT, 2026-10-02.** Answers `UrbanLens/infrastructure`'s
  `docs/handoffs/urbanlens-app-0.8.0-deploy-findings.md` (OPEN 2026-10-01). All three findings are real. Each is fixed
  on `release/v_0_9_0` and reaches `main` with v0.9.0.
- **Direction: outbound.** This repo to whoever owns `UrbanLens/infrastructure`.
- `id: N32` · `status: current`

## 1. `:main` moves only after CI passes; moving the dispatch alone would not have been enough

Your follower deploys whatever ghcr's `:main` names, and its scheduled tick reads `:main` too. Had only the dispatch
waited for CI, `:main` would still have moved at push time, and the next scheduled tick would have pinned it. So
`publish.yml` no longer runs on push. It runs on `workflow_run` of CI completing with `success` for a push to `main`
in this repository, and then:

- builds `:sha-<7>` for the commit CI tested;
- moves `:main` and sends the dispatch only if that commit is still `main`'s tip. A re-run of an older commit's CI must
  not move `:main` backwards. Your ancestry check would refuse to pin it, but anyone pulling `:main` would still get it;
- takes the image's `org.opencontainers.image.revision` label from the commit built. Under `workflow_run`,
  `github.sha` is `main`'s tip, not that commit.

A push to `main` no longer cancels the CI run of the push before it, so every commit on `main` gets a verdict.

**Not done: CI as a required check on `main`.** Release Please opens its PRs with `GITHUB_TOKEN`, which starts no
workflow, so a required CI check would block every release PR. Gating the tag does the job on its own.

**What it costs you:** a dispatch now arrives when CI finishes, about 1 h 40 m after the push on the last run. Two of
your files still describe publishing on push: `docs/runbooks/app-deploys.md`'s publish row (~2.5 min) and the path in
`.github/workflows/app-image.yml`'s header.

**Something your findings could not show:** the CI run for `d1fb1bf`, the commit production runs now, finished at
22:43Z and **failed**: 1 failed, 19,418 passed. The failure is `core/tests/test_version.py::
test_pyproject_is_preferred_source`, which compared pyproject's `0.8.0` against a hard-coded `0.8.0b0`. Release
Please's version bump broke the test, not the code. It is fixed on this branch (`c7f4a39e7`; the test now reads
pyproject). Under the new path, 0.8.0 would not have deployed until that fix landed.

## 2. 407 → None is what P181 intends; the step ships as migration 0034

Yes. Jess's ruling on P181 (2026-10-01) is that a shared Location is placed by containment alone, and a Location that
containment cannot place has no place. The command's own tests asserted exactly that. Nothing else changes for the
accounts involved. A pin keeps its stored type, and a stored BUILDING with no place still reads as a building. A wiki
keeps its own `place`. The 593 can only have come from the two `attach_location` calls in the pre-0.8.0 sweep:
`upsert_place` never clears an existing outline, and `resolve_for_point` never returns a place without one.

Why production's ratio differs from `v080e2e`'s was not measured, because this repo has no read access to production
rows. Whatever the cause, None is containment's answer, and containment's answer is the rule.

**One change from the command:** a Location left on no place is now unstamped (`place_resolved_at = NULL`), as
`detach_oversized_place` already does. The provider chain then treats the coordinate as never asked, and asks REData
about it on its next view. The 0.8.0 command stamped such a Location as asked just now, so it was asked again only
after the 10-minute retry window.

It ships as `src/urbanlens/dashboard/migrations/0034_reresolve_fiat_building_places.py`: a single `RunPython` after
`0033_v0_8_0_indexes`, irreversible (its reverse is a no-op, and your dry-run file is the undo record). Your migrate
PreSync hook runs it once per database. The management command is gone in 0.9.0. The 0.8.0 image you run still has
it, so a `--dry-run` there just before the 0.9.0 deploy gives you a fresher undo list if you want one.

## 3. Release images come from the same CI-gated run

When a published GitHub release's `v*` tag points at the commit CI passed, the same run adds the version tags. That is
`:0.9.0`, plus `:0.9` and `:latest` for a stable release, and it uploads the Python distributions to the release. It is
one build, so every tag of a commit is one digest, and the version label reads `0.9.0` rather than `main`. A release a
person publishes still builds at once, through the unchanged `release: published` trigger.

This also fixes a bug: metadata-action's default `latest=auto` added `:latest` to every `type=match` tag, prereleases
included, so the old `!prerelease` guard on `:latest` never held.

**0.8.0 has no version tags and will not get any automatically, because its CI failed** (above). Once this change is
on `main`, running the Publish workflow by hand with `ref: v0.8.0` would build them. Whether to tag an image whose CI
failed on a test-only fault is Jess's call. Until then, or until 0.9.0, `:latest` stays 0.7.0b0.

## Verified, and not

- The plan step's shell, run against the real repository: `d1fb1bf` gives tip, release `v0.8.0`; `ed9ab36` gives no
  release; `57a4a90` gives `v0.7.0b0`, not a prerelease; `8ec791e` gives `v0.6.0b0`, prerelease; a dispatch never
  moves `:main`. actionlint is clean on `publish.yml` and `ci.yml`.
- Migration 0034's tests (including that it refuses the same superseded, aggregate and implausible-domain places
  `resolve_for_point` refuses) and the oversized-place suite pass, on a database built fresh through 0034.
- **Not verified: a run on GitHub.** `workflow_run` workflows run only from the default branch, so the first real run is
  the first CI-green push to `main` after v0.9.0 merges. Its plan step prints the commit, whether `:main` moves, and the
  release it found.
