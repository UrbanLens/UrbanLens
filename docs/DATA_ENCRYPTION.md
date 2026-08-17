# Data Encryption Inventory

Tracks which fields containing personal/private data are encrypted at rest, which are
deliberately left plaintext (and why), what else on the box holds the same data, and what's
still open. Companion to `docs/NOTES.md`, `docs/designs/e2ee.md`.

## Scope: what this document covers, and what it deliberately does not

This inventory covers **identity, credential, contact, and interpersonal data** - the things a
user would not expect a database thief to read, and which the app does not need to query.

It deliberately **excludes the app's core location content**: pins, locations, trips, visits,
wikis, search history, and import failures. That is a
consequence of the queryability limit below: that content is matched with `icontains` global
search, PostGIS spatial predicates, uniqueness constraints, and ordering on nearly every page,
so column-level encryption is structurally unavailable for it. **Its at-rest protection is
delegated to disk/volume-level encryption on the host** (see the threat model). 

Two encryption layers exist in this codebase:

| Layer | What it protects | Key holder | Documented in |
|---|---|---|---|
| `EncryptedTextField` (Fernet, server-side) | The fields tabled below | **The server** | This document |
| E2EE message layer (X25519 + secretbox, client-side) | DM/group message bodies, safety archives | **The user's browser** | `docs/designs/e2ee.md` |

Only the second removes the server from the trust boundary. The first protects data that
travels *without* the server's key - dumps, replicas, an insider's `SELECT`.

## Mechanism

`dashboard.models.fields.EncryptedTextField` (a `TextField` subclass) encrypts with Fernet
(symmetric, authenticated) on write and decrypts on read. Values are **written** under the
active key (`UL_FIELD_ENCRYPTION_KEY`, else Django's `SECRET_KEY`) and **readable** under any
key in `UL_FIELD_ENCRYPTION_KEY_FALLBACKS` as well — a `MultiFernet`, so a key change is a
rolling change rather than an outage. See the field's own docstring for the full rationale.

Django's `SECRET_KEY` is always tried as a final fallback. That is deliberate: every install
predating `UL_FIELD_ENCRYPTION_KEY` has its data encrypted under it, so **setting that variable
for the first time is safe** and does not orphan existing rows.

The key is derived as a single unsalted `SHA256` of the configured secret (`_derive_fernet`).
There is no stretching, so **key strength is input strength** — hence the length/alphabet floor
enforced in `settings/app.py` (`_reject_weak_encryption_keys`). A Fernet token carries its own
HMAC, so one stolen ciphertext row lets an attacker verify key guesses offline at hashing speed;
a human-chosen passphrase would fall quickly.

**What this buys you:** protection against DB dumps/backups/read replicas/insider queries
exposing the raw value at rest.

**What it does not buy you:** protection from anything with app-level DB access (a compromised
app server can always ask the ORM to decrypt), and it is **not queryable** — no `.filter()`,
`.exclude()`, `.order_by()`, DB index, or uniqueness constraint can operate on ciphertext,
because Fernet output includes a random IV/timestamp, so the same plaintext never encrypts to
the same ciphertext twice. A field can only be switched to `EncryptedTextField` if nothing in
the codebase does an exact-match lookup, sort, aggregate, or DB-level uniqueness/index check
against it. If a field mixes both needs (e.g. an email that must both stay secret *and* be
matched against), keep the matchable form plaintext (usually normalized/hashed) and encrypt
only the display copy — see `ProfileEmail` below for a worked example.

## Threat model

Field encryption is scoped to artifacts that travel **without** the host's key material. Being
explicit about the rest:

| Scenario | Outcome today |
|---|---|
| Stolen DB dump / read replica / insider `SELECT` | ✅ Tabled fields are ciphertext. This is the case the mechanism is built for. |
| Compromised app server (RCE, malicious dependency) | ❌ The app holds the key by necessity; it can decrypt anything. Also defeats E2EE prospectively, since the server ships the JS. |
| **Stolen disk / physical seizure of the host** | ⚠️ See below. |
| Malicious/compromised operator | ❌ for field encryption; ✅ retrospectively for E2EE message bodies (an operator can backdoor future JS, but cannot read history they never had keys for). |

### Physical control of the server

An attacker who images the disk gets the database **and**, in the default single-box deployment,
the key that decrypts it: `UL_FIELD_ENCRYPTION_KEY`/`DJANGO_SECRET_KEY` live in the repo-root
`.env` on the same disk as the `postgres-data`, `backups`, `media_volume`, and `logs` volumes.
This is inherent to any deployment where the app must possess its own key to run — field
encryption cannot solve it, and claiming otherwise would be false comfort.

What actually reduces the blast radius, in descending value for a small self-hosted install:

1. **Full-disk encryption on the host** (LUKS on the data disk, or moving `/var/lib/docker` plus
   the compose project directories onto a LUKS-backed mount). This is the single highest-value
   control: it protects the database, media, backups, logs, image layers, and the `.env` itself
   in one move, with zero application changes. Nothing in this repo can do it for you, and no
   application-level scheme substitutes for it.
2. **Keep secrets out of build artifacts.** `.dockerignore` excludes `.env*`;
   before that, `COPY . /app` baked every secret into an image layer, where it survives
   rotation and is recoverable with `docker history`/`docker save` from any dangling image.
   Runtime injection now comes from compose's `env_file:`.
3. **Encrypt backups asymmetrically.** `pg_dump` output is plain `.sql` on a same-host volume
   (`core/controllers/backups/db.py`), and backups are the artifact most likely to travel. Piping
   through `age -r <recipient>` (private half kept offline) means no decryption secret exists on
   the box at all. There is also no offsite copy today, so losing the host loses the database and
   every backup together.
4. **Separate the encryption key from the signing key** (see below).

What survives disk theft today: **E2EE message ciphertext only**. `MessagingKeyBundle` stores
key material exclusively in client-wrapped form. The caveat is that `password_wrapped_secret` is
offline-attackable and its Argon2id parameters are libsodium's *interactive* preset (opslimit 2,
memlimit 64 MiB) — tuned for login latency, not for a blob an attacker can grind on indefinitely.
The recovery-wrapped copy is full-entropy and safe.

## Key management

`UL_FIELD_ENCRYPTION_KEY` should be set explicitly and be **independent of `DJANGO_SECRET_KEY`**.

Why independence matters: with `UL_FIELD_ENCRYPTION_KEY` unset, the field-encryption key *is*
`SECRET_KEY`, which also signs sessions, CSRF tokens, and password-reset links. The standard
response to a suspected cookie-forgery incident — "rotate `SECRET_KEY`" — then silently swaps the
active Fernet key, and every pre-rotation row becomes unreadable unless the operator also
remembers to move the old value into `UL_FIELD_ENCRYPTION_KEY_FALLBACKS`. The two keys protect
different assets on different cadences and should not be the same secret.

## Rotating the encryption key

Never swap the key in one step — that is the data-loss path. Roll it:

1. **Add the new key, retire the old one.** Set `UL_FIELD_ENCRYPTION_KEY` to the new value and
   move the previous value into `UL_FIELD_ENCRYPTION_KEY_FALLBACKS` (comma-separated). Deploy.
   Nothing breaks: new writes use the new key, existing rows still read under the fallback.
   *When rotating `SECRET_KEY` itself, the **old** `SECRET_KEY` is what goes in the fallback
   list — only the current one is implicit.*
   The strength floor (32 characters, 16 distinct) applies to the **active key only**. A retired
   key that would be refused as the active one is accepted here and logs a warning — otherwise an
   install running a weak key could not boot the settings module that `rotate_field_encryption`
   needs, leaving it stuck between an unbootable app and abandoning its encrypted rows.
2. **Re-encrypt everything.**
   ```bash
   python manage.py rotate_field_encryption --dry-run   # reports what would change
   python manage.py rotate_field_encryption
   ```
   It discovers every `EncryptedTextField` column from the app registry (so fields added later
   are covered automatically), rewrites each value under the active key, and **exits non-zero if
   any row could not be decrypted** — do not proceed past a failure, and do not drop a key until
   this completes cleanly. The whole run is one transaction, so a crash mid-run rolls back
   rather than leaving a half-rotated database. It reads through a raw cursor, so `fail_soft`
   fields cannot degrade to empty and be written back as such.
3. **Drop the retired key** from `UL_FIELD_ENCRYPTION_KEY_FALLBACKS`. Deploy.

To verify step 2 really worked, remove the old key and confirm the app still reads the data —
that is exactly what `test_field_encryption_rotation.py` asserts.

> **Backups outlive rotations.** A dump taken before a rotation is encrypted under the *old*
> key. Retain every retired key offline for as long as any backup encrypted under it exists, or
> that backup is unrestorable. Backups are likewise exempt from in-app deletion guarantees: a
> row scrubbed today is still in last week's dump.

**Retrofitting encryption onto a field that already has data**: swapping the field's Python
type to `EncryptedTextField` does not touch existing rows — Django's `from_db_value`/
`get_prep_value` only run through the ORM, so raw bytes already in the column stay exactly as
they were. Reading a pre-existing plaintext row through the new field raises `InvalidToken`
(or, for `fail_soft` fields, degrades to empty — worse, because the value looks deleted rather
than broken). The migration must encrypt existing values with a raw-cursor data migration that
runs independently of the ORM's field-type state — see `_encrypt_column` in
`migrations/0048_encrypt_preference_and_contact_label.py` (the current reference implementation,
which additionally **skips already-encrypted values so the pass is idempotent**; the earlier
0007/0039 versions double-encrypt if re-run). Because this bypasses Django's model layer
entirely, operation order relative to the `AlterField` in the same migration doesn't matter.

## Encrypted fields

| Model | Field | Purpose | Since |
|---|---|---|---|
| `TOTPDevice` | `secret` | TOTP 2FA seed | pre-existing |
| `FlickrAccount` | `oauth_token`, `oauth_token_secret` | Flickr OAuth 1.0a credentials | pre-existing |
| `GooglePhotosAccount` | `access_token`, `refresh_token` | Google OAuth2 credentials | pre-existing |
| `GoogleCalendarAccount` | `access_token`, `refresh_token` | Google OAuth2 credentials | pre-existing |
| `ImmichAccount` | `api_key` | Immich server API key | pre-existing |
| `SiteSettings` | `notify_gotify_token` | Gotify push token (**`fail_soft` since 2026-08-13**, see below) | pre-existing |
| `Profile` | `phone_number`, `signal_username`, `discord_username`, `whatsapp_number`, `telegram_username`, `matrix_handle` | Contact methods, gated by `contact_visibility` at the app layer | 2026-08-09 |
| `Profile` | `bio`, `area` | Free-text self-description / rough location | 2026-08-09 |
| `ProfileEmail` | `email` | Secondary email, display copy (see note below) | 2026-08-09 |
| `ProfileNote` | `content` | Private note one user keeps about another | 2026-08-09 |
| `EmergencyContactDefault` | `email` | Reusable safety-checkin contact template | 2026-08-09 |
| `GooglePhotosAccount` | `google_email` | Connected account email, display only | 2026-08-09 |
| `GoogleCalendarAccount` | `google_email` | Connected account email, display only | 2026-08-09 |
| `Profile` | `additional_preferences`, and the seven `*_preference_other` fields (`photo_taking`, `photo_sharing`, `photo_tagging`, `photo_usage`, `friend_request`, `meetup`, `exploring_with_others`) | Free-text interaction preferences — same class as `bio`, missed in the original pass | 2026-08-15 |
| `EmergencyContactDefault` | `label` | Names a third party who never consented to being in this database; its sibling `email` was already encrypted | 2026-08-15 |
| `FriendInvitation` | `message` | The inviter's free-text note to someone who does not yet have an account. `email` stays plaintext — see below | 2026-08-15 |

### `fail_soft`: which fields degrade, and what "the row is left intact" means

Every field added on 2026-08-09 and 2026-08-15 is declared `fail_soft=True`. The pre-existing
credential fields are not, **with one exception**: `SiteSettings.notify_gotify_token` became
`fail_soft=True` on 2026-08-13 (migration 0040). It is shaped like a credential but sits in
neither camp — no caller anywhere catches `InvalidToken` and drops it, and `SiteSettings` is a
singleton that three context processors load on every render, so an undecryptable token made
every page 500 *including the styled 500 page itself*. Note its default is
`os.getenv("UL_GOTIFY_TOKEN", "")`, so it degrades to the environment token when one is set,
not necessarily to empty. Covered by `test_site_settings_encrypted_degradation.py`.

The two categories differ because a credential can be re-fetched from its provider while user-authored content cannot. Of the five `InvalidToken` handlers in this codebase, **four self-heal by deleting
the row** — Immich, Flickr, Google Photos, Google Calendar. TOTP deliberately does not; silently dropping a user's own 2FA factor is a bigger security-posture change than
dropping a stale third-party connection.

> **Limit:** this covers fields with a **string default** only. A `null=True` field degrades to
> `None`, which cannot carry an attribute (`x is None` is not overridable), so `Profile.bio`,
> `Profile.area`, `GooglePhotosAccount.google_email`, `GoogleCalendarAccount.google_email`, and
> `EmergencyContactDefault.email` still lose their ciphertext on the next full save. **Declare
> new `fail_soft` content fields as `blank=True, default=""`, not `null=True`.** Converting the
> existing five is a follow-up.

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
| `SafetyContactOptOut.email` | Opt-out identity | `Q(email__iexact=email)` exact-match is the entire suppression mechanism (`SafetyContactOptOutManager.blocks_notification`); genuinely indexed (`idxdb_scoo_email`) |
| `SafetyCheckinContact.email` | Per-checkin contact snapshot | Matched against `SafetyContactOptOut` by value at notify time. *(Corrected 2026-08-15: this row previously claimed the column was indexed. It is not — only `SafetyContactOptOut.email` is. The value-matching justification stands on its own.)* |
| `SafetyCheckinContact.name` | Third party's name on a live check-in | Snapshot on an active check-in; scrubbed by the post-resolution archival design rather than encrypted |
| `ProfileNickname.nickname` | Private nickname a viewer assigns another user | Global search matches it with `nickname__icontains` — see `services.global_search.providers.person_match`, which powers every "from &lt;person&gt;" clause. **This one was encrypted and then reverted**: ciphertext made the lookup silently return nothing, breaking two existing tests (`test_matches_sharer_by_viewers_own_nickname`, `test_messages_from_person_matches_viewers_own_nickname`) with no error raised |
| `SocialLink.handle` | Public social media handle | User-published-by-design (rendered as a public profile link, not gated by a visibility setting like contact info is); also has a test asserting an exact-match `.filter(handle=...)` |
| `PushDevice.address` | UnifiedPush endpoint URL / FCM token | `register_device()` does `update_or_create(profile=, address=)` and a DB `UniqueConstraint(profile, address)` — Fernet ciphertext is non-deterministic, so both the idempotent re-registration and the constraint would silently break (duplicate rows per device instead of one). Fixable, but needs a separate deterministic lookup column (e.g. an HMAC-SHA256 of the address) added first — see Follow-ups |
| `FriendInvitation.email` | A **non-member's** email address | `EmailField(db_index=True)`, exact-matched at signup to link an invitation to the new account. Needs the `ProfileEmail` split or an HMAC column — see Follow-ups. Its sibling `message` was encrypted on 2026-08-15 |
| `DirectMessage.body` | Plaintext DM fallback (mutually exclusive with `ciphertext`, the E2EE path) | Real chat history already in the DB; blind field-type swap would `InvalidToken` on every existing row without a data migration, and tests do `.filter(body=...)` exact-match lookups. Not "obvious" — see Follow-ups |
| `GroupMessage.body` | Plaintext group-chat fallback | Same `body`-xor-`ciphertext` contract as `DirectMessage`. *(Added 2026-08-15 — absent from the original audit entirely, so a pass scoped from the old follow-up list would have encrypted DMs and left group chats plaintext.)* |
| `SafetyCheckinMessage.body` | Safety check-in chat | Same reasoning as `DirectMessage.body`; note the check-in family already has a deliberate post-resolution encryption design (`SafetyCheckinArchive`) rather than continuous encryption — see Follow-ups |
| `NotificationLog.message` | First ~120 chars of a DM/group message, as a notification preview | Not queried, so it is encryptable — but the deeper problem is retention, not encryption: these rows have no FK to the message, so the DM hard-delete sweep never purges them and the excerpt outlives the "deleted" message forever. See Follow-ups |
| `Profile.birth_date` | Date of birth | Not string-typed — no `EncryptedDateField` exists yet, would need a new field class; not filtered/queried anywhere today so it's safe, just not "obvious" in one pass — see Follow-ups |
| `AccountKdf.auth_salt` | Argon2id salt | Not confidential by design — a KDF salt's job is to be unique, not secret; encrypting it adds no protection |
| `WebAuthnCredential.credential_id`, `public_key` | Passkey identifiers | Not secrets — `credential_id` is a public handle, `public_key` is (by definition) public asymmetric key material |
| `MessagingKeyBundle.*` | E2EE key material | Already client-encrypted; `public_key` is public by design. Server-side encryption would add nothing |
| `Image.exif_data`, GPS columns | Full EXIF dump incl. device serials; photo coordinates | Coordinates drive map queries; the EXIF blob is retained wholesale. Trimming to a useful allowlist is the better fix than encryption — see Follow-ups |
| `DeviceScan` / `DeviceSignalReading` | MAC addresses and profile-attributed GPS trails | Matched and clustered by value/geometry. High-sensitivity, low-retention-value raw rows — needs a retention policy more than encryption; see Follow-ups |
| `PinImportFailure.maps_url` | A URL the user tried to import | Core location content, per the scope statement above |
| `django.contrib.auth.User.email/first_name/last_name` | Django's built-in auth fields | Encrypting these needs a custom auth backend/admin/social-auth-pipeline rework across all of Django auth — too invasive to be "obvious"; see Follow-ups |
| `social_django` (`UserSocialAuth.access_token/refresh_token/id_token/extra_data`) | OAuth tokens from Google/Discord | Third-party table (`social-auth-app-django`) we don't define — see Follow-ups |
| `oauth2_provider` (`AccessToken`/`RefreshToken`/`Grant`, `Application.client_secret`) | External-API bearer credentials | Third-party table (django-oauth-toolkit). Live, long-lived credentials stored in plaintext — strictly more dangerous than the `social_django` row above, and absent from this document until 2026-08-15. See Follow-ups |

## Other stores holding the same data

Column encryption protects one copy. These hold others:

| Store | Contents | Protection |
|---|---|---|
| `django_session` (DB + Valkey, `cached_db`) | Pending TOTP secrets during enrolment, freshly minted API keys, backup codes at reveal time, assistant chat state | Signed, **not encrypted**. Forging needs `SECRET_KEY`; *reading* does not |
| Valkey (cache, Celery broker, channels layer) | Sessions, task payloads, WS broadcasts incl. message bodies and live GPS | Plaintext but **memory-only** — `--save ""` and `--appendonly no` mean nothing reaches disk. Keep it that way, keep it off the public network |
| `backups` volume | Full `pg_dump` `.sql` | Encrypted fields dump as ciphertext; everything else is readable. Unencrypted at the file level, same host — see the threat model |
| `media_volume` | Original user photos (the core sensitive asset) | None at rest; nginx serves it directly |
| `logs` volume | App logs and daphne access logs (URL paths carry pin/trip slugs) | None at rest |
| `NotificationLog` rows | Plaintext DM/group message excerpts | None, and never purged — see Follow-ups |

## Follow-ups

Ordered roughly by risk if left as-is:

1. **`NotificationLog.message` retention** — a plaintext excerpt of every non-E2EE DM and group
   message, kept forever with no FK to the message, so "delete for everyone" and the
   disappearing-message sweep both leave it behind. The same string is POSTed to third-party
   UnifiedPush endpoints. Fix the retention first (purge with the message, or stop storing
   body-derived text), then encrypt what remains.
2. **`oauth2_provider` tokens** — live bearer/refresh credentials for the external API, in
   plaintext. Options: django-oauth-toolkit's hashed-token storage, or a digest lookup column
   mirroring the `ApiKey` `prefix`+`hash` pattern already used for PATs.
3. **`DirectMessage.body` / `GroupMessage.body` / `SafetyCheckinMessage.body`** — the
   highest-value *content* target, but requires a data migration over every existing row,
   fixing the tests that filter by exact `body=`, and a product call on whether plaintext
   fallback should exist at all now that E2EE is the real mechanism. A deliberate pass, not a
   silent field-type change.
4. **Nullable `fail_soft` fields lose their ciphertext on save** — convert `Profile.bio`,
   `Profile.area`, both `google_email` columns, `EmergencyContactDefault.email`, and
   `FriendInvitation.message` from `null=True` to `blank=True, default=""` so
   `UndecryptableValue` covers them.
5. **Deterministic-lookup companions** — `PushDevice.address` and `FriendInvitation.email` both
   need an HMAC-SHA256 column to carry the exact-match/uniqueness role before the value itself
   can be encrypted.
6. **Encrypt backups + get an offsite copy** — `age`/GPG public-key encryption in
   `DatabaseBackup.run()`, private half offline. Also the only fix for "the host burns down".
7. **Session-stored secrets** — encrypt the pending TOTP secret before it enters the session
   (or key it server-side by a random nonce). Correct the `TOTPDevice` and assistant-controller
   docstrings that claim session data "never reaches the database"; under `cached_db` it does.
8. **`Image.exif_data`** — trim to an allowlist (capture time, camera model, orientation)
   instead of retaining serial numbers and the rest of the deanonymization surface.
9. **Device-scan retention** — MAC addresses plus profile-attributed movement trails are the
   highest-sensitivity/lowest-utility rows here; drop readings older than the clustering
   lookback window rather than encrypting them.
10. **`Profile.birth_date`** — build an `EncryptedDateField` (store as an encrypted ISO string,
    parse back to `date`) following the same pattern as `EncryptedTextField`.
11. **`social_django` OAuth tokens** — third-party table; options are a custom storage/pipeline
    wrapper, or DB/volume-level encryption. Needs its own design.
12. **`django.contrib.auth.User.email/first_name/last_name`** — out of scope without a custom
    auth backend; flagging only so it isn't mistaken for an oversight.

## Adding a new sensitive field

Before adding a plaintext PII field, ask:

1. Is it ever matched exactly, sorted, aggregated, or uniqueness/indexed at the DB level?
   - **No** → use `EncryptedTextField`.
   - **Yes** → keep the matched form plaintext; if you also need to *display* the value,
     consider storing a separate encrypted display copy (see `ProfileEmail` above).
2. If you are encrypting it, can the value be re-fetched from somewhere else?
   - **Yes** (a credential) → leave `fail_soft` off so callers can detect the failure and drop
     the row, prompting the user to reconnect. **Unless** nothing catches `InvalidToken` and the
     model loads on ordinary page renders — then it must be `fail_soft` regardless of being a
     credential, or one bad row takes the site down (`notify_gotify_token` is the precedent).
   - **No** (user-authored content) → set `fail_soft=True`, and declare it
     `blank=True, default=""` rather than `null=True` so a degraded read keeps its ciphertext.
3. Update this document in the same PR.

### Before encrypting an *existing* field, prove it isn't queried

Encrypting a queried field fails **silently** — no exception, just a lookup that stops matching.
Do not rely on reading the model or on a scoped grep. Run all of these repo-wide, from `src/`:

```bash
rg '\b<field>__'                  # any lookup: __icontains, __iexact, __in, ...
rg '(filter|exclude|get|get_or_create|update_or_create|values|values_list|order_by|annotate|aggregate)\([^)]*\b<field>\b'
rg '<field>' -g '**/filterset.py' -g '**/admin.py' -g '**/serializer*.py'   # search_fields, filterset_fields
```

Watch for generic field names: `label` appears in ~79 query call sites across unrelated models,
so scope the check to the owning model before concluding anything.

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
