# Data Encryption Inventory

Tracks which fields containing personal/private data are encrypted at rest, which are
deliberately left plaintext (and why), and what's still open. Companion to `docs/NOTES.md`
and `CLAUDE.md`. Audited 2026-08-09; key-rotation support added 2026-08-10 — verify against the
code before relying on specifics.

## Mechanism

`dashboard.models.fields.EncryptedTextField` (a `TextField` subclass) encrypts with Fernet
(symmetric, authenticated) on write and decrypts on read. Values are **written** under the
active key (`UL_FIELD_ENCRYPTION_KEY`, else Django's `SECRET_KEY`) and **readable** under any
key in `UL_FIELD_ENCRYPTION_KEY_FALLBACKS` as well — a `MultiFernet`, so a key change is a
rolling change rather than an outage. See the field's own docstring for the full rationale.

Django's `SECRET_KEY` is always tried as a final fallback. That is deliberate: every install
predating `UL_FIELD_ENCRYPTION_KEY` has its data encrypted under it, so **setting that variable
for the first time is safe** and does not orphan existing rows.

**What this buys you:** protection against DB dumps/backups/read replicas/insider queries
exposing the raw value at rest.

> ### ⚠️ What a lost key costs, and why the stakes changed
>
> Every field encrypted *before* 2026-08-09 was a **re-obtainable credential** — lose the key
> and the user reconnects Google, re-enrols TOTP, re-pastes an API key. That is why the five
> `InvalidToken` handlers in this codebase (Immich, Flickr, Google Photos, Google Calendar,
> TOTP) all "self-heal" by **deleting the row** — lossless for a credential.
>
> The fields added on 2026-08-09 are **user-authored content with no external source of
> truth**: a bio, private notes about other users, contact details, emergency contacts.
> Delete-to-heal would be data destruction, and raising would take `Profile` — loaded on nearly
> every authenticated request — down site-wide. So those fields use `fail_soft=True`: they read
> as empty, log loudly, and **leave the row intact** so a recovered key can still restore them.
>
> Key changes are survivable as long as you follow the rotation procedure below. Skipping it is
> what turns a routine key change into permanent loss.

## Rotating the encryption key

Never swap the key in one step — that is the data-loss path. Roll it:

1. **Add the new key, retire the old one.** Set `UL_FIELD_ENCRYPTION_KEY` to the new value and
   move the previous value into `UL_FIELD_ENCRYPTION_KEY_FALLBACKS` (comma-separated). Deploy.
   Nothing breaks: new writes use the new key, existing rows still read under the fallback.
   *When rotating `SECRET_KEY` itself, the **old** `SECRET_KEY` is what goes in the fallback
   list — only the current one is implicit.*
2. **Re-encrypt everything.**
   ```bash
   python manage.py rotate_field_encryption --dry-run   # reports what would change
   python manage.py rotate_field_encryption
   ```
   It discovers every `EncryptedTextField` column from the app registry (so fields added later
   are covered automatically), rewrites each value under the active key, and **exits non-zero if
   any row could not be decrypted** — do not proceed past a failure, and do not drop a key until
   this completes cleanly.

   If the failure is a row whose key is genuinely gone — a leftover from an earlier incident,
   not the key you are retiring — that row can never decrypt again, and waiting will not change
   that. Re-run with `--skip-undecryptable`, which lists those rows and completes anyway:

   ```bash
   python manage.py rotate_field_encryption --skip-undecryptable
   ```

   Use it only once you have confirmed the listed rows are unrecoverable rather than a missing
   fallback you could still supply. Everything else is rotated exactly as normal, so the retired
   key is safe to drop; the listed rows were already unreadable before you started.
3. **Drop the retired key** from `UL_FIELD_ENCRYPTION_KEY_FALLBACKS`. Deploy.

To verify step 2 really worked, remove the old key and confirm the app still reads the data —
that is exactly what `test_field_encryption_rotation.py` asserts.

**What it does not buy you:** protection from anything with app-level DB access (a compromised
app server can always ask the ORM to decrypt), and it is **not queryable** — no `.filter()`,
`.exclude()`, `.order_by()`, DB index, or uniqueness constraint can operate on ciphertext,
because Fernet output includes a random IV/timestamp, so the same plaintext never encrypts to
the same ciphertext twice. A field can only be switched to `EncryptedTextField` if nothing in
the codebase does an exact-match lookup, sort, aggregate, or DB-level uniqueness/index check
against it. If a field mixes both needs (e.g. an email that must both stay secret *and* be
matched against), keep the matchable form plaintext (usually normalized/hashed) and encrypt
only the display copy — see `ProfileEmail` below for a worked example.

**Retrofitting encryption onto a field that already has data**: swapping the field's Python
type to `EncryptedTextField` does not touch existing rows — Django's `from_db_value`/
`get_prep_value` only run through the ORM, so raw bytes already in the column stay exactly as
they were. Reading a pre-existing plaintext row through the new field raises `InvalidToken`.
The migration must encrypt existing values with a raw-cursor data migration that runs
independently of the ORM's field-type state — see `encrypt_existing_tokens` /
`_encrypt_column` in `migrations/0007_pinshare_bundled_with_markup_map_removed_flags.py` for
the established pattern (reused for every field encrypted below): `SELECT` the raw value with
a plain cursor, encrypt it in Python with a bare `EncryptedTextField().get_prep_value(...)`,
`UPDATE` it back. Because this bypasses Django's model layer entirely, operation order
relative to the `AlterField` in the same migration doesn't matter for correctness.

## Encrypted fields

| Model | Field | Purpose | Since |
|---|---|---|---|
| `TOTPDevice` | `secret` | TOTP 2FA seed | pre-existing |
| `FlickrAccount` | `oauth_token`, `oauth_token_secret` | Flickr OAuth 1.0a credentials | pre-existing |
| `GooglePhotosAccount` | `access_token`, `refresh_token` | Google OAuth2 credentials | pre-existing |
| `GoogleCalendarAccount` | `access_token`, `refresh_token` | Google OAuth2 credentials | pre-existing |
| `ImmichAccount` | `api_key` | Immich server API key | pre-existing |
| `SiteSettings` | `notify_gotify_token` | Gotify push token | pre-existing |
| `Profile` | `phone_number`, `signal_username`, `discord_username`, `whatsapp_number`, `telegram_username`, `matrix_handle` | Contact methods, gated by `contact_visibility` at the app layer | 2026-08-09 |
| `Profile` | `bio`, `area` | Free-text self-description / rough location | 2026-08-09 |
| `ProfileEmail` | `email` | Secondary email, display copy (see note below) | 2026-08-09 |
| `ProfileNote` | `content` | Private note one user keeps about another | 2026-08-09 |
| `EmergencyContactDefault` | `email` | Reusable safety-checkin contact template | 2026-08-09 |
| `GooglePhotosAccount` | `google_email` | Connected account email, display only | 2026-08-09 |
| `GoogleCalendarAccount` | `google_email` | Connected account email, display only | 2026-08-09 |

**Every field added on 2026-08-09 is declared `fail_soft=True`**; the pre-existing credential
fields are not. See the key-loss callout above for why the two categories differ — in short, a
credential can be re-fetched from its provider so its row is safe to drop, and user-authored
content cannot, so its row must be preserved even when unreadable.

**`ProfileEmail` split**: `normalized_email` stays plaintext `CharField` with a `db_index` and a
conditional unique constraint (`is_verified=True`) — invite-by-email lookup, duplicate checks,
and username-or-email login all do exact-match queries against it, and Postgres cannot enforce
uniqueness or index ciphertext. Only `email` (the display copy) is encrypted. Same logic
applies to `Profile.primary_email_normalized` (indexed cache of `User.email`, used for the same
lookups) — left plaintext, see the "reviewed, left plaintext" table below.

## Reviewed, deliberately left plaintext

| Model.field | Contains | Why it's not encrypted |
|---|---|---|
| `Profile.primary_email_normalized` | Normalized copy of login email | `db_index=True`, exact-matched for friend-invite/dup-check/login lookups |
| `ProfileEmail.normalized_email` | Normalized secondary email | `db_index` + conditional unique constraint; exact-matched the same way |
| `SafetyContactOptOut.email` | Opt-out identity | `Q(email__iexact=email)` exact-match is the entire suppression mechanism (`SafetyContactOptOutManager.blocks_notification`) |
| `SafetyCheckinContact.email` | Per-checkin contact snapshot | Indexed; matched against `SafetyContactOptOut` by value at notify time |
| `ProfileNickname.nickname` | Private nickname a viewer assigns another user | Global search matches it with `nickname__icontains` — see `services.global_search.providers.person_match`, which powers every "from &lt;person&gt;" clause. **This one was encrypted and then reverted**: ciphertext made the lookup silently return nothing, breaking two existing tests (`test_matches_sharer_by_viewers_own_nickname`, `test_messages_from_person_matches_viewers_own_nickname`) with no error raised |
| `SocialLink.handle` | Public social media handle | User-published-by-design (rendered as a public profile link, not gated by a visibility setting like contact info is); also has a test asserting an exact-match `.filter(handle=...)` |
| `PushDevice.address` | UnifiedPush endpoint URL / FCM token | `register_device()` does `update_or_create(profile=, address=)` and a DB `UniqueConstraint(profile, address)` — Fernet ciphertext is non-deterministic, so both the idempotent re-registration and the constraint would silently break (duplicate rows per device instead of one). Fixable, but needs a separate deterministic lookup column (e.g. an HMAC-SHA256 of the address) added first — see Follow-ups |
| `AccountKdf.auth_salt` | Argon2id salt | Not confidential by design — a KDF salt's job is to be unique, not secret; encrypting it adds no protection |
| `WebAuthnCredential.credential_id`, `public_key` | Passkey identifiers | Not secrets — `credential_id` is a public handle, `public_key` is (by definition) public asymmetric key material |
| `django.contrib.auth.User.email/first_name/last_name` | Django's built-in auth fields | Encrypting these needs a custom auth backend/admin/social-auth-pipeline rework across all of Django auth — too invasive to be "obvious"; see Follow-ups |
| `social_django` (`UserSocialAuth.access_token/refresh_token/id_token/extra_data`) | OAuth tokens from Google/Discord | Third-party table (`social-auth-app-django`) we don't define — see Follow-ups |
| `DirectMessage.body` | Plaintext DM fallback (mutually exclusive with `ciphertext`, the E2EE path) | Real chat history already in the DB; blind field-type swap would `InvalidToken` on every existing row without a data migration, and tests do `.filter(body=...)` exact-match lookups. Not "obvious" — see Follow-ups |
| `SafetyCheckinMessage.body` | Safety check-in chat | Same reasoning as `DirectMessage.body`; note the check-in family already has a deliberate post-resolution encryption design (`SafetyCheckinArchive`) rather than continuous encryption — see Follow-ups |
| `Profile.birth_date` | Date of birth | Not string-typed — no `EncryptedDateField` exists yet, would need a new field class; not filtered/queried anywhere today so it's safe, just not "obvious" in one pass — see Follow-ups |

## Follow-ups (not done in this pass — need a design decision, not just a field swap)

Ordered roughly by risk if left as-is:

1. **`DirectMessage.body` / `SafetyCheckinMessage.body`** — highest-value target (real message
   content), but requires: a data migration re-encrypting every existing row (irreversible if
   botched), fixing the handful of tests that filter by exact `body=` value, and a product call
   on whether plaintext-fallback DMs should even keep existing today given E2EE (`ciphertext`)
   is already the "real" privacy mechanism for messaging. Recommend a deliberate pass, not a
   silent field-type change.
2. **`social_django` OAuth tokens** — third-party table; options are a custom storage/pipeline
   wrapper, or DB/volume-level encryption. Needs its own design.
3. **`PushDevice.address`** — add a deterministic HMAC column for the `update_or_create`/unique
   lookup, encrypt the raw address, keep the HMAC indexed+unique instead.
4. **`Profile.birth_date`** — build an `EncryptedDateField` (store as an encrypted ISO string,
   parse back to `date`) following the same pattern as `EncryptedTextField`.
5. **`django.contrib.auth.User.email/first_name/last_name`** — out of scope without a custom
   auth backend; flagging only so it isn't mistaken for an oversight.

## Adding a new sensitive field

Before adding a plaintext PII field, ask:

1. Is it ever matched exactly, sorted, aggregated, or uniqueness/indexed at the DB level?
   - **No** → use `EncryptedTextField`.
   - **Yes** → keep the matched form plaintext; if you also need to *display* the value,
     consider storing a separate encrypted display copy (see `ProfileEmail` above).
2. If you are encrypting it, can the value be re-fetched from somewhere else?
   - **Yes** (a credential) → leave `fail_soft` off so callers can detect the failure and drop
     the row, prompting the user to reconnect.
   - **No** (user-authored content) → set `fail_soft=True`, so an unreadable value degrades to
     the field default instead of raising, and the row is never auto-deleted.
3. Update this document in the same PR.

### Before encrypting an *existing* field, prove it isn't queried

Encrypting a queried field fails **silently** — no exception, just a lookup that stops matching.
Do not rely on reading the model or on a scoped grep. Run all of these repo-wide, from `src/`:

```bash
rg '\b<field>__'                  # any lookup: __icontains, __iexact, __in, ...
rg '(filter|exclude|get|get_or_create|update_or_create|values|values_list|order_by|annotate|aggregate)\([^)]*\b<field>\b'
rg '<field>' -g '**/filterset.py' -g '**/admin.py' -g '**/serializer*.py'   # search_fields, filterset_fields
```

Check `Exists()`/`OuterRef()` subqueries specifically — that is where the `ProfileNickname`
regression hid, in a different app's service (`global_search`) than the model it queried.

Then **run the full test suite**, not a hand-picked subset. The regression above was missed
because the verification batch was chosen by guessing which files looked related; the tests
that caught it lived in `test_global_search_engine.py`, which nothing about the model would
have pointed you to. Note also that a negative-assertion test (`assertEqual(results, [])`)
still passes when the feature is entirely dead — passing tests around the field are not
sufficient evidence on their own.

## Migration rollbacks decrypt (2026-08-15)

The two in-place encryption migrations (`0007` tokens, `0039` contact/note fields) carry real
decrypting reverses: `migrate` below either one restores plaintext for every Fernet-shaped value
(recognised by the `gAAAA` prefix; never-encrypted rows pass through untouched) and **aborts the
whole rollback** if any value cannot be decrypted under the configured keys - refusing beats
silently writing garbage where pre-migration code expects plaintext. Before 2026-08-15 both
reverses were no-ops, which made a rollback succeed while corrupting every encrypted column.
