# D24 — Google SSO links stay keyed by email address; address recycling is accepted, and an unverified address never signs in

`id: D24` · `status: accepted` · `updated: 2026-09-29` · `decided by: Jess, 2026-09-29 (and in an earlier session)`

**This is Jess's ruling, not an agent's proposal.** Do not reopen it without asking her.

## Decision

- Google links keep `social_core`'s default uid, the email address. `SOCIAL_AUTH_GOOGLE_OAUTH2_USE_UNIQUE_USER_ID`
  stays unset, and links are never re-keyed to Google's `sub`.
- Users must be able to sign in by email address even after registering through SSO. How SSO accounts
  are stored or indexed doesn't otherwise matter.
- A company reassigning an address to someone else (P152, N29 G3-31) can't be solved here, and is accepted.
- The one hard rule: an **unverified** address never signs in to the account holding it.
  `pipeline.refuse_unverified_address_link` runs before `social_user` and refuses a sign-in whose link is keyed
  by an address the provider did not verify (`tests/hypothesis/test_google_link_identity.py`).

## History

An agent switched links to `sub` with a re-keying pipeline step on 2026-09-29 (a90cf21a7), against Jess's
earlier direction. It was reverted the same day, before reaching staging or production. The unverified-address
guard is the part of that change Jess's rule requires, so it was kept in a smaller form.

## Open for Jess

- The guard refuses only when a link for that address exists, so someone holding an unverified copy of an
  address at Google can learn that an UrbanLens account signed up through Google with it. Refusing every
  unverified-address Google sign-in would close that but block new Google sign-ups whose address Google
  hasn't verified, which `resolve_sso_email` supports today.
- An SSO-only account that never set a password can't sign in by email: a password-reset request sends it a
  "sign in with your provider" hint instead of a reset link (`SsoAwarePasswordResetForm`, UL-257). A signed-in
  SSO user can set a password at `account.set_password`.
