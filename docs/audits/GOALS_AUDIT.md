# Goals audit: docs/GOALS.md vs. existing documentation

Generated 2026-08-24 by comparing `docs/GOALS.md` against `README.md`, `docs/FEATURES.md`,
`docs/ROADMAP.md`, `docs/DATA_ENCRYPTION.md`, `docs/NOTES.md`, and `docs/PROBLEMS.md`.

**Scope**: this is a documentation-only comparison — it checks what the docs *claim*, not what
the code actually does. Each item below is a candidate for a follow-up pass that reads the code
and decides whether the code, the docs, or `GOALS.md` needs to change. Do not assume the code
matches whichever side of a contradiction below sounds current — verify before touching
anything.

## Contradictions (docs claim something goals.md says shouldn't be true)

1. **DMs are not E2EE-only.** `docs/FEATURES.md` documents group chats as supporting "text
   (plaintext or E2EE)," and `docs/DATA_ENCRYPTION.md` describes `DirectMessage.body` as a
   real, first-class "Plaintext DM fallback (mutually exclusive with `ciphertext`)" — not a
   deprecated/legacy path. `docs/GOALS.md` says E2EE should be as close to unconditionally enforced as
   possible with no way to turn it off. Needs a decision: kill the plaintext fallback, or was
   goals.md overstating the intent?

2. **Shared pin photos aren't independent copies.** `docs/PROBLEMS.md` records a real incident:
   "Deleting your own photo silently broke it for everyone you shared it with" — the recipient's
   `Image` row points at the same storage key as the sender's, not a duplicate. The pin *record*
   is copied on accept; the attached photo file is not. `docs/GOALS.md`'s "recipient never gets access
   to the sender's actual pin" is violated at the media layer specifically.
   NOTE From Jess: This is a problem. The photo should be copied to the recipient, not directly accessible. Provenance should still be trackable.

3. **Trip activities may reference pins directly, not the Wiki.** `docs/ROADMAP.md` §2.4
   describes trip-activity exposure in terms of recording provenance when a pin is exposed to
   other trip members — i.e., the current design references the pin and mitigates the leak via
   `record_share_exposure`, rather than having activities source display data from the
   associated Wiki as goals.md specifies. The share-tracking half is fine; the "source from
   Wiki, not pin" half is not how it's built today.
   NOTE From Jess: record_share_exposure is important and must be maintained. However, it should probably reference a place, a boundary, or a wiki instead of a user's private pin. "source from wiki, not pin" should be implemented for trip activities.

4. **Backups are not encrypted at rest.** `docs/DATA_ENCRYPTION.md` states the backup volume is
   currently "Unencrypted at the file level, same host," with encrypting backups still an open
   follow-up item. `docs/GOALS.md` states this as a requirement, not a nice-to-have.

5. **Log rotation/purge is an open TODO, not shipped.** `docs/ROADMAP.md` §3.4 tracks this as
   unresolved (UL-136).

6. **Pre-commit's double-run is documented as intentional, not a bug.** `CLAUDE.md` describes
   the current double pre-commit run as deliberate ("first silent, so fixes are applied before
   real failures are output"). `docs/GOALS.md` calls this friction to fix. Worth confirming with Jess
   whether the documented rationale is actually satisfactory, or whether the workflow should
   still change regardless of why it currently works this way.
   NOTE From Jess: The double run is a fine compromise temporarily, but that doesn't make it ideal. It just makes it workable for right now.

## Gaps (goals.md states something no doc currently mentions)

7. **"Concealed users" / per-field concealed wiki data.** No doc uses this terminology or
   describes behavior-pattern-based flagging that hides specific wiki fields from a user. Either
   this is a genuinely new goal, or it shipped under different naming and `docs/FEATURES.md` is
   missing it — needs Jess to clarify (also flagged as an open question in `GOALS.md`).
   NOTE From Jess: This has already shipped under a different branch, and any agent reading this should see it in the repo under the branch they're working in.

8. **Public-pin seeding tool for empty-map deployments.** Not mentioned in any doc. If this
   exists in some form already, `docs/FEATURES.md` doesn't describe it; if it doesn't exist yet,
   it's a net-new roadmap item.
   NOTE From Jess: This is a new item, not something we've already implemented.

9. **Decrypt-on-login / discard-key-on-logout baseline for pin encryption.** `DATA_ENCRYPTION.md`
   explains *why* pins/locations/wikis are currently out of scope for encryption (search,
   PostGIS predicates, ordering) — consistent with goals.md's framing of search as the main
   obstacle — but the proposed baseline step isn't recorded anywhere as a planned mitigation.

10. **E2EE for private photos, private notes, and markup maps.** Not on any roadmap. Per
    goals.md these are called out as having *no* real search tradeoff (photo bytes and notes
    aren't searched), making them lower-effort than pin encryption — worth prioritizing ahead of
    the harder pin-encryption problem precisely because nothing blocks them.

11. **Gameplay data stored in an ML-model-agnostic schema.** `docs/FEATURES.md` describes
    Consensus's trust model (Beta-Bernoulli) and the Facts confidence engine as tied to their
    current implementations, not framed as a swappable/general data layer.

12. **"Browse alone / search alone" as an explicit design principle.** Both capabilities exist
    (Memories, saved filters, lists; global search) but no doc states the "either path should
    fully suffice on its own" bar as a goal to audit new pages/features against.

13. **Filter-backed list reconciliation + undo UI.** `docs/PROBLEMS.md` documents a related but
    distinct smart-list bug (a `root_pins()` omission causing count mismatches) — not the
    manual-vs-filter-membership reconciliation and missing undo UI that goals.md describes. If
    the reconciliation logic itself doesn't exist yet, this is a build item, not just a UI gap.

14. **Encryption-defeat tests (property/brute-force-timing style).** No doc mentions testing the
    encryption implementation adversarially — only that E2EE/at-rest features exist.

15. **Success+failure assertions and "every page has a test" as explicit written rules.** Neither
    is stated as a house rule anywhere (CLAUDE.md's testing section covers Hypothesis/mocking
    conventions but not this). Worth codifying in CLAUDE.md if Jess confirms it as a standing
    rule, so it's enforced going forward rather than living only in GOALS.md.
    NOTE From Jess: This is confirmed as a goal.

16. **i18n motivation and roadmap.** No doc mentions the team's relocation to Europe or a
    translation effort, though Django's i18n scaffolding (`gettext`, `LANGUAGE_CODE`) already
    exists per a recent `docs/PROBLEMS.md` entry — the infrastructure isn't a from-scratch
    build, just unused for translation yet.

## Notes (consistent, or exceeds the stated goal — no action implied)

- Pin privacy default, wiki-earned-access-by-boundary-pin, and safety check-in defaults
  (no live location, token-based non-member access) are all solidly matched by
  `docs/NOTES.md`, `docs/ROADMAP.md` §1.3, and `docs/FEATURES.md` — including the "404 for
  both nonexistent and unauthorized" construction-level enforcement goals.md asks for.
- Games sourcing shared content from the Wiki (not pins) is already built this way per
  `docs/FEATURES.md`'s SpotGuessr/Consensus descriptions.
- Consistent map controls is an active, named effort (`docs/ROADMAP.md` flags violations as
  anti-patterns, e.g. UL-210) rather than a finished state — treat as in-progress, not done.
- Public-pin criteria (strict, rarely triggers) match `docs/FEATURES.md` and
  `docs/ROADMAP.md` §4.4 closely.
- Self-hosting without Kubernetes is confirmed by the plain `docker compose` setup described
  in `README.md`; no doc describes federation/P2P, consistent with it being purely aspirational.

## Suggested next step

Per the process Jess described: once `docs/GOALS.md` has been reviewed/edited, hand the
contradictions above (not the gaps) to a fresh session to read the actual code for each one and
determine what — code, docs, or the goal itself — needs to change. The gaps are better handled
as a separate `docs/FEATURES.md` completeness pass (a full read-through of the codebase against
the feature list), since several of them may already be partially implemented under different
names.
