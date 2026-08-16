# Key recovery by data class: strict E2EE for messages, a handed-out vault key for everything else

Status: **messaging half implemented** (2026-08-15; third revision of the design — the filename
is historical). Extends `docs/designs/e2ee.md`, whose sections now describe the shipped passkey
layer. Supersedes both same-day drafts: the PRF-primary draft and the escrow-by-default draft.
Shipped: `E2EEPasskeyWrap` + `WebAuthnCredential.is_login_factor` (migration 0049), the
passkey-wrap endpoints, PRF injection into registration/2FA-login ceremonies, the client
enroll/unlock flows and passkey-first unlock dialog, the 2FA ride-along, and the monthly
passkey-or-password prompt replacing the per-session set-password nag. Still design-only: the
class-2 vault (ships with photos) and the safety-archive migration onto it.

## Requirements (consolidated from both reviews, 2026-08-15)

- **R1 — messages are fully E2EE, and losing them is acceptable.** "Direct Messages should still
  be fully e2ee. Losing messages is fine if a user loses anything (device, password, etc.)."
  This kills the second draft's escrow of the messaging identity — the server must never hold a
  path to DM plaintext, and the design no longer has to guarantee message recovery.
- **R2 — other encrypted data does not need true E2EE.** "The server can hand out keys initially,
  as long as the server no longer has access to them after generation." Server-side *generation
  and initial distribution* of a key is fine; server-side *retention* is not.
- **R3 — losing a device must not lose non-message data.** "Losing your phone cannot result in
  losing access to photos." A design needing the old device present is unacceptable. A design allowing recovery with an old device is acceptable, as long as it is rare and/or there are other pathways to recovery.
- **R4 — recovery keys may exist in flows, but must never be load-bearing.** "Providing recovery
  keys to users is okay… it's just not something we should be counting on them saving, because
  most users don't."
- **R5 — no forced 2FA;** passkey/SSO-provider trust is acceptable; opt-in "maximum security"
  is a liked pattern.

## The taxonomy these requirements produce

The two earlier drafts each tried to give *one* answer for *all* encrypted data, and each broke a
requirement somewhere. The requirements actually describe **three data classes with three
different trust models**, and the design gets simple once they're named:

| Class | Examples | Who can ever read it | Loss on total key loss |
|---|---|---|---|
| **1. Messages** | DM/group bodies | Participants only — server never | Acceptable (R1) |
| **2. Vault** | Photos-at-rest (planned), safety archives | The user; server only transiently (R2) | **Not acceptable** (R3) |
| **3. Operational** | `EncryptedTextField` fields: contact info, tokens, bios | The server, ongoing — it must act on them with the user absent (emergency-contact emails fire at a deadline, OAuth tokens refresh in Celery) | N/A — server always has the key |

Class 3 already exists and is correctly designed for what it holds (`docs/DATA_ENCRYPTION.md`);
R2 is a ceiling ("E2EE not needed"), not a floor, for data the server must read on its own
schedule. Nothing changes there. This document is about classes 1 and 2.

## Class 1 — Messages: keep strict E2EE, add convenience, accept loss

The current system already is the right system: X25519 identity, password/recovery wraps, sealed
conversation/group keys, server never able to read. No escrow, no tiers — every user is
"maximum security" for messages by construction. What changes is only around the edges:

- **The PRF passkey wrap returns as the recommended convenience** (it was the first draft's
  centerpiece, demoted when message recovery had to be guaranteed; with loss acceptable it is
  exactly what it should be — a cheap way to make loss *rare* without being load-bearing).
  Mechanism unchanged from the first draft: wrap the identity private key under
  `HKDF(prf_output)` from a WebAuthn `prf` assertion; per-credential `E2EEPasskeyWrap` rows
  (credential FK, `prf_input`, `wrapped_secret`, `bundle_version`); unlock passkeys decoupled
  from login 2FA via `WebAuthnCredential.is_login_factor` (satisfying R5 — enrolling one never
  conscripts the user into a login prompt); client-challenged, non-server-verified ceremonies.
  Browser support: Chrome/Edge 116+, Safari 18+, Firefox 135+; `webauthn~=3.0.0` needs no change.
- **Cold-device unlock ladder**, in order: password (if set — already self-heals every login) →
  passkey PRF (if enrolled) → recovery key (allowed to appear, per R4, just never the only exit)
  → accept loss: reset keys, partners keep their own copies, new messages encrypt under a fresh
  key. The dialog copy should say the last part plainly — "your old messages stay with the people
  you sent them to" is true and takes the sting out.
- **The per-session set-password interstitial dies anyway.** Its stated purpose was giving SSO
  accounts a message-unlock path; with loss acceptable that purpose no longer justifies a
  every-session nag that trains users to dismiss prompts. Replace with a one-time (or monthly)
  prompt offering *either* a passkey or a password, then stay quiet.
- Keep: silent OAuth enrollment, `navigator.storage.persist()` (both shipped), the recovery key
  shown once and viewable in Settings.

## Class 2 — Vault: the handed-out key

A per-user symmetric **vault key**, for data that must survive everything short of the user
abandoning every credential they have.

**Generation and distribution (the "handed out initially" model, R2).** The server generates the
vault key at first need (e.g. first photo upload after this ships). It immediately:

1. hands it to the requesting client over TLS, which caches it (IndexedDB) and wraps it under
   whatever the user has — password-derived key, passkey PRF, recovery key — uploading the wraps
   exactly like `MessagingKeyBundle` does today (`VaultKeyBundle`: `password_wrapped`,
   `recovery_wrapped`, passkey wraps; same salts/KDF machinery, reused not reinvented);
2. keeps a **bootstrap escrow copy** (wrapped under a dedicated `UL_VAULT_ESCROW_KEY` env secret,
   never in the DB) **only until the account has at least one durable wrap** — a password, a
   passkey wrap, or an explicit "I saved my recovery key" confirmation — then deletes it.

That bootstrap escrow is the literal reading of R2 — *"hand out keys initially, as long as the
server no longer has access after"* — and it is what makes R3 hold during the window where a
brand-new SSO user has no password, no passkey, and an unsaved recovery key. Once a durable wrap
exists, the server deletes its copy and provably loses access; Settings shows which state the
account is in ("your vault key is recoverable by the server until you add a password or passkey"
vs "held only by you"). **Opt-in maximum security (R5) = delete the escrow now**, accepting that
recovery then depends entirely on the user's own wraps.

For users who never establish a durable wrap, the escrow simply persists — an honest default for
exactly the users who would otherwise lose data, visible in Settings, gone the moment they
graduate. There is no way to satisfy "server forgets" and "user who saved nothing recovers"
simultaneously; this design makes the trade per-user and visible instead of picking one global
answer.

**Recovery on a new device**: password → PRF passkey → recovery key → bootstrap escrow (if still
held; session-only endpoint, notification email on every release, rate-limited) → nothing left.
With the wrap ladder plus escrow-until-durable, reaching "nothing left" requires declining every
wrap, losing every device, *after* explicitly confirming recovery-key custody — at that point the
loss is a kept promise, not an accident.

**What the vault key encrypts is out of scope here** — the photos design must still answer
server-side processing (clamd scanning, EXIF/GPS stripping, thumbnailing, keywording all need
plaintext at upload; serving `<img>` needs plaintext at request). The likely shape is
session-provisioned keys: the client hands the vault key to the server at session start, the
server holds it only in Valkey (already memory-only, `--save "" --appendonly no`) so at rest the
server stores only wrapped copies. That doc owns those decisions; this one only guarantees the
key's lifecycle: generated anywhere, durable with the user, forgotten by the server.

**Safety archives should migrate to the vault key.** Today `SafetyCheckinArchive` seals each
archive to the *messaging* identity (`archive_checkin` → `MessagingKeyBundle.public_key`), which
made sense when that was the only user-held key — but under R1 the messaging identity is now
officially loss-acceptable, and a resolved check-in's record is exactly the kind of thing a user
may need *after* losing a phone. Archives are class-2 data sealed with a class-1 key. When the
vault ships, new archives seal to it; existing archives can be re-sealed opportunistically from
any device that can unseal them (same pattern as the existing reset-flow rewrap).

## What each class survives

| Event | Messages (1) | Vault (2) | Operational (3) |
|---|---|---|---|
| Stolen DB dump | ✅ sealed | ✅ sealed (escrow key in env, wraps user-held) | ✅ ciphertext (field key in env) |
| Full box compromise / operator | ✅ history sealed (JS-backdoor caveat unchanged) | ✅ after bootstrap; ❌ during it (visible in Settings) | ❌ readable — by design, the server must read it |
| Lost phone, no other credentials | ❌ messages gone — accepted (R1) | ✅ escrow or wraps recover it | ✅ server-side |
| Lost *everything*, saved nothing, escrow deleted | ❌ | ❌ — explicitly chosen, twice | ✅ |

`docs/designs/e2ee.md` keeps its threat model **unchanged** for messages — that document's
strongest claims stay true for class 1, which is the cleanest outcome of this revision. The vault
gets its own honest-limits section when it ships.

## Build order

1. ~~Retire the per-session set-password nag in favor of a one-time prompt.~~ **Shipped
   2026-08-15**: `PostLoginRedirectView` now prompts only accounts with no password AND no
   passkey, and "Not now" snoozes for 30 days on the profile
   (`Profile.credential_prompt_snoozed_until`), not per session. *(persist() hardening and the
   rewrap KDF-floor fix also shipped 2026-08-15.)*
2. ~~The PRF passkey wrap for messages.~~ **Shipped 2026-08-15** — see the status block above
   and `docs/designs/e2ee.md` for the as-built description.
3. **With photos:** `VaultKeyBundle` + bootstrap escrow + the wrap ladder, then the photos
   at-rest design on top; migrate safety-archive sealing.

---

## Appendix: superseded second draft (escrow-by-default for messages)

Kept for the record: the 2026-08-15 second draft proposed a server-held escrow of the *messaging*
identity key as the default tier, on the premise that message loss was unacceptable for SSO
users. The subsequent review corrected the premise — messages must stay fully E2EE and their loss
is acceptable — which moves the escrow idea to where it now lives: a *bootstrap-only* mechanism
for the vault class, deleted once the user holds a durable wrap. The per-tier honesty table and
the session-only/notified/rate-limited guardrails from that draft carry over to the vault's
bootstrap escrow unchanged.
