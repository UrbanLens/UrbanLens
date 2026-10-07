---
status: accepted
date: 2026-09-29
---

# Map search geocodes from the browser, with full as-you-type autocomplete

Formerly `D25`. Detail: [`docs/designs/browser-direct-geocoding.md`](../designs/browser-direct-geocoding.md).

This is Jess's ruling; do not reopen it without asking her. Location search, the add-pin address box and the markup map's title suggestion call Nominatim straight from the browser, which keeps geocoding traffic off UrbanLens's servers. As-you-type autocomplete stays fully live. Enforcing a third party's terms of use is not this codebase's job: a feature that looks like it breaks one is written up for Jess, not degraded.

## Considered options

- A cached, rate-limited server proxy with cache-only suggestions: an agent shipped it citing Nominatim's usage policy, and it was reverted the same day. A proxy adds server load rather than removing it.

## Consequences

- The Nominatim URL is hardcoded to the public instance. Pointing it at the self-hosted Nominatim needs that instance's URL, which nothing in the repos names yet.
