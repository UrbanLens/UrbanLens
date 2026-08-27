# Data-encryption doc vs. code: audit

Generated 2026-08-25, checking `docs/DATA_ENCRYPTION.md` for drift since its last review
(2026-08-15 re-audit, commit `e64c392b`). Ten days of active, concurrent multi-agent development
had passed, including this session's own substantial external-API/OAuth2 work - the right question
wasn't "is this document right" (it already carries two prior passes) but "has anything moved
since the last time someone checked."

**How to read status:** `MATCHES` = the code does exactly what the doc says, today. `PARTIAL` =
mostly true with a real caveat. `STALE` = the doc was accurate once but the code has since changed.
`CONTRADICTS` = the code does something different from what the doc claims, today.

## Summary

| Topic | Status |
| --- | --- |
| Encrypted-fields table + fail_soft pattern + InvalidToken handlers | `MATCHES` |
| Mechanism / key management / rotation | `PARTIAL` - one real regression found |
| Follow-ups #4, #7, #10 (the three mechanical-looking ones) | `MATCHES` - all still genuinely open |
| Plaintext table + Follow-ups drift (oauth2_provider, message bodies, HMAC gap, NotificationLog) | `PARTIAL` - one pre-existing imprecision found |

**No new privacy/security gap was introduced in the last ten days.** Every field, every fail_soft
flag, every InvalidToken handler, and every one of the twelve Follow-up items is exactly where the
2026-08-15 audit left it - a genuinely clean result for the highest-stakes doc in this series.

## Mechanism / key management / rotation - `PARTIAL`, one regression fixed

The Fernet/MultiFernet fallback chain, the unsalted-SHA256 key derivation, the migration-rollback
decrypt behavior, and the active-key-only strength floor all match current code exactly.

One real, dateable regression: `rotate_field_encryption` gained a `--skip-undecryptable` flag on
2026-08-16 (commit `70655b47`), documented in the same commit - then the very next day's large
release-branch merge (`3fcd6ab3`) resolved this section of the doc by taking the *older* upstream
wording wholesale, silently dropping the new paragraph. The flag and its test
(`test_skip_undecryptable_finishes_the_rotation`) both survived in code; only the documentation of
it was lost. **Fixed**: the `--skip-undecryptable` paragraph is restored in the "Rotating the
encryption key" section.

## Follow-ups #4, #7, #10 - `MATCHES`, all still open

All three items flagged as candidates for a direct, unambiguous fix turned out to still be exactly
as documented:

- **#4** (nullable `fail_soft` fields losing ciphertext on save): still `null=True` on all five
  fields. Confirmed this needs more than a field-attribute edit - `null=True` -> `NOT NULL` is a
  real column constraint change, and Django doesn't auto-backfill existing NULLs when tightening
  one. The local dev DB has zero rows in all five tables (not zero NULLs - genuinely empty), so
  this couldn't be fully risk-assessed against real data. **Left unfixed this round** - it needs an
  actual migration with a defensive backfill step, which is more than "finish what's already in
  flight" scope.
- **#7 (docstring half)**: `TOTPDevice`'s and the assistant controller's docstrings both claimed
  session-stored secrets "never reach the database," which is wrong under the `cached_db` session
  backend. **Fixed**: both docstrings corrected in place (zero behavior change) to say what's
  actually true - the state does land in `django_session`, it's just unmodeled and not queried.
- **#10** (`Profile.birth_date` needs an `EncryptedDateField`): still an unencrypted `DateField`,
  still never queried anywhere in the codebase (confirmed by grep - no filter/order_by/annotate
  hits). The doc's risk framing is unchanged. Not implemented - it needs a new field class, out of
  scope for a drift check.

## Plaintext table / other Follow-ups - `PARTIAL`, one pre-existing imprecision fixed

`NotificationLog` retention, `oauth2_provider` AccessToken/RefreshToken/Grant plaintext storage,
the missing `PushDevice`/`FriendInvitation` HMAC companion columns, and the `DirectMessage`/
`GroupMessage`/`SafetyCheckinMessage` body-vs-ciphertext contract are all exactly as documented -
including after this session's own extensive external-API/OAuth2 work, which touched scope/schema/
serializer code but never token storage.

One pre-existing imprecision (not new drift - the underlying code hasn't changed since
2026-07-30): the plaintext table's `oauth2_provider` row bundled `Application.client_secret` in
with the genuinely-plaintext AccessToken/RefreshToken/Grant tokens. django-oauth-toolkit hashes
`client_secret` by default, and this project's one real `Application` row (the first-party mobile
client) is provisioned with a deliberately empty secret rather than a real credential - so there's
no live confidential-client secret actually sitting in plaintext today. **Fixed**: the table row
now separates the two.

## Fixes applied (2026-08-25)

1. `docs/DATA_ENCRYPTION.md` - restored the `--skip-undecryptable` documentation lost in the
   2026-08-17 merge.
2. `docs/DATA_ENCRYPTION.md` - separated `Application.client_secret` from the genuinely-plaintext
   `oauth2_provider` token row, with the reason it's not actually exposed.
3. `src/urbanlens/dashboard/models/account/model.py` - corrected `TOTPDevice`'s docstring re:
   session data reaching the database.
4. `src/urbanlens/dashboard/controllers/assistant.py` - corrected the module docstring re: the
   same claim.

## Open questions for Jess

None new. Follow-up #4's added scope (needs a real migration + defensive backfill, not just a
field-attribute edit) is a sizing note for whoever picks it up next, not a design ambiguity.

## Not yet audited

- Follow-ups #1, #2, #3, #5, #6, #8, #9, #11, #12 were spot-checked for "has this been silently
  resolved" (none had) but not re-derived from scratch - the 2026-08-15 audit's analysis of *why*
  each is still open wasn't independently re-verified, only its current-truth.
