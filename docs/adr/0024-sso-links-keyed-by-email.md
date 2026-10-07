---
status: accepted
date: 2026-09-29
---

# Google SSO links stay keyed by email address

Formerly `D24`. Detail: [`docs/designs/sso-links-keyed-by-address.md`](../designs/sso-links-keyed-by-address.md).

This is Jess's ruling; do not reopen it without asking her. Google links keep `social_core`'s default uid, the email address: `SOCIAL_AUTH_GOOGLE_OAUTH2_USE_UNIQUE_USER_ID` stays unset and links are never re-keyed to Google's `sub`. A user who registered through SSO must still be able to sign in by email. A company reassigning an address to someone else is accepted as unsolvable here. The one hard rule: an unverified address never signs in to the account holding it (`pipeline.refuse_unverified_address_link`).

## Considered options

- Re-keying links to `sub`: an agent shipped it against Jess's direction, and it was reverted the same day.
