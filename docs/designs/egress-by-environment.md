# D26 — Production spends the shared external budgets; staging gets a sliver, development none

`id: D26` · `status: accepted` · `updated: 2026-10-05` · `decided by: Jess, 2026-10-05`

> **Written by a Claude agent. Not authoritative.**
>
> The rule is Jess's; the mechanism and the classification are an agent's. Re-read the code before relying on a
> table here: `urbanlens/UrbanLens/egress.py` and `dashboard/services/core/egress.py` win when they disagree.

**This is Jess's ruling, not an agent's proposal.** Do not reopen it without asking her:

> Dev should be mocking replies, using cached replies, going through redata prod so replies can be cached, going
> through other services, or not making requests at all. Staging can make some requests, but shouldn't be making
> very many, and they should ideally be cached by production redata when possible... Production should consume
> the bulk of any of our budgets, across the board for all services.

## Why

Every UrbanLens host shares one residential address with production REData. A keyless public API's tolerance is
per address, and a billed API's free tier is per account, so development and staging calls spend production's
budget. The 2026-10-05 egress audit found development sending 13,291 calls in a week, including 441 background
Google Geocoding calls that fell through from a REData 503, Save Page Now writes to the Internet Archive, and real
mail through SMTP2GO. `UL_ALLOW_OUTBOUND_APIS` could not express "REData yes, third parties no", and seven LLM
paths bypassed it.

## Decision

Every external service is classified, and one policy decides per environment, at the point every call passes
through (`rate_limiter._reserve_call`, `api_call_slot`, the gateway session, and `service_is_enabled`):

| Category | production | staging | development, local |
|---|---|---|---|
| `redata`, `internal` | allowed | allowed | allowed |
| `quota`, `billed` | budget × `UL_ENVIRONMENT_SHARE` | budget × share | refused (share 0) |
| `ai` | allowed, logged | allowed, logged | allowed, logged |
| `messaging` | real | console / no-op | console / no-op |
| `public_write` | allowed | refused | refused |

- **The share.** `UL_ENVIRONMENT_SHARE` replaces `UL_BILLED_API_SHARE`. Defaults: production 0.9, staging 0.05,
  development and local 0, an unknown environment 0, the test suite 1.0. It scales every window (per minute, per
  day, per 30 days) of every `quota` and `billed` service, never below one call; a billed service's monthly
  ceiling keeps #210's formula with this share (R31). An *unset* `UL_ENVIRONMENT` is production
  (`environments/meta.py`), so every other deployment must set it: one that loses the variable spends
  production's share, sends real mail and schedules every beat entry, and says so only in its startup line.
  The share is per deployment: every deployment that says `staging` takes its own 5%, so with more than two
  of them (the damballa staging stack, k3s staging, and the k3s sites while they run as staging before the
  cutover) production's 0.9 and theirs together pass the whole budget. Give the extra ones
  `UL_ENVIRONMENT_SHARE=0`, or a smaller share.
- **Trying a provider.** `UL_ENVIRONMENT_SHARE_OVERRIDES=nominatim=0.02,sms=1` gives one service its own share. For
  `messaging` and `public_write` any share above 0 opts the service in, and 0 turns it off on production. The
  overrides are logged at startup, with a warning for a name nothing classifies. REData, our own hosts and AI
  ignore overrides; the admin switch on their `ApiRateLimit` row turns them off.
- **A refusal is not a failure and not a deferral.** It raises `EnvironmentRefusedError`, a non-transient
  `ServiceDisabledError`, so every caller that already degrades on a switched-off service keeps working. It writes
  no `ApiCallLog` row and logs once per service every ten minutes (as #209's refused inputs do). It is never
  recorded as unanswered or held against provider health. Its `is_outage` stays true in the one sense that
  property is documented for, "nothing was learned", which is what keeps every cache from storing it as an empty
  answer; a non-outage error there would be cached by, or re-raised from, the ~40 sites that branch on it.
  - The boundary chain skips the source; it does not defer and schedule a retry.
  - A panel says "Not available in this environment" and stores nothing (`external_data.unavailable_here`), even
    when its source swallowed the refusal: `collect_refusals` sees it either way. A slide carousel (satellite,
    street level) shows what its allowed providers found, and trusts that pass for 30 minutes instead of 12
    hours when one was refused, so an override takes effect.
  - Off production the name and forward-geocode chains never fall through from REData to a direct `quota` or
    `billed` provider (`egress.direct_fallback_permitted`). A REData 503 is reported as unavailable: no name for
    now, which is retried, or `AddressLookupUnavailableError` (503) for an address, never "no such place".
- **Background work that reaches outside runs on production only.** Every beat entry is classified in
  `BEAT_EGRESS`; off production only `internal` entries are scheduled, plus `UL_BACKGROUND_TASKS_ALLOWLIST`. Each
  `external` task also checks for itself (`@external_background_task`), so a chained or re-enqueued run stops too.
- **Mail** goes to Django's console backend off production, whatever `UL_EMAIL_BACKEND` says, unless
  `UL_EMAIL_SEND_OUTSIDE_PRODUCTION=true`. Sign-up codes, verification and magic links are then read from the
  web and worker logs.
- **The hosted Protomaps basemap** is production's: elsewhere `UL_PROTOMAPS_API_KEY` is ignored and the street
  and dark layers stay on the self-hosted mirror (and the CSP does not admit the hosted glyph host).
- **Retired:** `UL_ALLOW_OUTBOUND_APIS`, `UL_BILLED_API_SHARE`, and `UL_DEMO_MODE`'s REData-only exemption. Demo
  mode keeps its login endpoint and banner; a demo deployment that should call only REData sets
  `UL_ENVIRONMENT_SHARE=0` (AI stays allowed). The share does not hold `messaging` or `public_write`, so a demo
  runs under a non-production `UL_ENVIRONMENT` (`docs/DEMO.md`); on production they stay on. A set retired
  variable is warned about at startup.

## How the services are classified

`ServiceDefaults.category` on every registry and plugin entry; `UNREGISTERED_SERVICES` for keys with no defaults
of their own; `UNLEDGERED_SERVICES` for paths that do not go through a gateway session. `test_egress_classification`
fails on any service, gateway, literal service key or beat entry left unclassified. At runtime an unclassified
service is treated as `billed`.

| Category | Services |
|---|---|
| `redata` | every `redata_*` key (37 registered, plus `redata_boundary`, `redata_json`, `redata_location_context`, `redata_place_details`); `digital_commonwealth` and the street-level and archive labels `mapillary`, `kartaview`, `panoramax`, `smithsonian`, `library_of_congress`, `internet_archive`, `chronicling_america`, whose calls REData makes |
| `internal` | `overpass` (the self-hosted primary), `ollama`, `immich`, `ai_inference`, `gotify`, `clamd` |
| `quota` | `nominatim`, `wikipedia`, `wikipedia_media`, `wikimedia`, `wayback_machine` (reads), `overture_maps`, `google_open_buildings`, `microsoft_building_footprints`, `overpass_public_mirror` (the public fallbacks, on a session of their own), `open_historical_map`, `open_meteo`, `openweathermap`, `osrm`, `census_tigerweb`, `usgs`, `esri`, `basemap_vendor_tiles`, `flickr`, `hibp`, `virustotal`, `google_photos`, `google_street_view_metadata`; unledgered `google_oauth_refresh`, `google_oauth_connect`, `social_sign_in`, `flickr_oauth`, `user_url_fetch`, `github_contributors`, `git_fetch` |
| `billed` | `google_geocoding`, `google_places`, `google_maps`, `azure_maps`, `apple_maps`, `google_earth`, `protomaps_basemap` |
| `ai` | `trivia_generation`, `trivia_moderation`, `trivia_answer_check`, `trivia_wiki_incorporation`, `article_expansion`, `article_safety`, `link_extraction`, `document_pin_import`, `trip_suggestions`, `label_style_suggestions`, `category_suggestions`, `assistant`, `ai_photo_keywords`, `cloudflare_image_classifier` |
| `messaging` | `email`, `sms`, `whatsapp` (and their base `twilio`), `unified_push` |
| `public_write` | `wayback_save` (Save Page Now), `google_calendar` (event writes; its imports are refused with it), `stripe` |

The seven AI features that called the provider with no gate now each reserve an `api_call_slot` under their
feature name, one row per provider call (the assistant one per round), which the gateway fills in with model,
tokens and cost (#214). The six rows added for them carry only a per-minute guard against a runaway loop: AI
is logged, never refused for spend (R31). `trivia_generation` already had a row, 5 a minute and 200 a day,
which its slot now enforces; the hourly sweep stops at the first refused call and marks no wiki tried, so the
rest wait for the next run rather than for the 30-day retry.

### Paths outside the gateway session

| Path | Category | How it is held |
|---|---|---|
| `email` | messaging | `EMAIL_DELIVERY_BACKEND`: console off production unless `UL_EMAIL_SEND_OUTSIDE_PRODUCTION` |
| `unified_push` | messaging | `push.send_push_to_devices` asks before each batch, recording nothing against the devices |
| `stripe` | public_write | `stripe_client.is_configured` and `configure` ask before any SDK call; off production billing reads as unconfigured |
| `google_oauth_refresh` | quota | the calendar and photos gateways ask for their own service before refreshing a token |
| `google_oauth_connect`, `social_sign_in`, `flickr_oauth` | quota | **not held**: a person signing in or connecting their own account |
| `user_url_fetch` | quota | **not held**: a URL a person supplied, fetched for them (link pages, gallery photos, remote tile templates, social-link checks, avatars) |
| Wikipedia lead image as a pin cover | quota | `wiki_seed._store_cover_from_url` asks for `wikimedia` before downloading |
| `manage.py diagnose_places_api` | billed | asks for `google_places` before its raw requests |
| `github_contributors`, `git_fetch` | quota | **not held**: the thanks page (cached) and the site-admin update check |
| `gotify`, `clamd`, `ai_inference` | internal | self-hosted |

### Beat entries

| | Entries |
|---|---|
| `external` (production only, gated in the task too) | `scheduled-location-enrichment`, `scheduled-trivia-generation`, `scheduled-trivia-wiki-incorporation`, `scheduled-redata-public-locations-sync`, `stripe-subscriptions-sync` (and its chained reconcile), `wayback-archive-sweep`, `calendar-push-sweep` |
| `internal` (every environment) | the other 39 |

`external` means the task's job is to call out. A task whose job is this deployment's own state stays `internal`
even when it mails, pushes, texts or asks a provider on a user's behalf: the safety check-in sweeps (an overdue
check-in must still turn overdue), the SpotGuessr and consensus stall sweeps (a stalled game must still end), the
outbox drain, upload retries, provider health and account deletion. Their messaging is console or nothing off
production, and every provider call they make is held by the call-level policy.

## What development and staging call after this

**Development** (share 0): REData, the self-hosted Overpass primary, the tile cache, Ollama, `ai_inference` and AI
providers (logged). Nothing in `quota` or `billed`: no Nominatim, Wikipedia, Wayback, Overture, Open Buildings,
Esri tiles, Google, Azure or Protomaps. No mail (console), texts, push, Calendar writes, Stripe or Save Page Now.
Of beat, only internal maintenance. Visible consequences: the Esri satellite basemap layer and other vendor raster
layers are grey; panels from direct third parties say "Not available in this environment"; the Settings geocode
answers 503; the assistant's weather and routing tools, which call OpenWeatherMap and OSRM directly, report
unavailable. `UL_ENVIRONMENT_SHARE_OVERRIDES` opts a provider back in for a session's work.

**Staging** (share 0.05): REData, our own hosts and AI as development; a twentieth of every `quota` and `billed`
window (Nominatim's 500 a day becomes 25; Google Geocoding's free-tier share 200 a month), but never as a
fallthrough from a failed REData call. No mail, texts, push, Calendar, Stripe or Save Page Now. Of beat, only
internal maintenance. k3s staging's Celery runs (beat, worker and panels worker at 1/1/1, read from the cluster
on 2026-10-05; the infrastructure repo's `docs/STATUS.md` says it is off), so this policy is what holds it.

**Development's REData calls spend production REData's budget**: every deployment points `UL_REDATA_API_URL` at
production REData, which reaches billed and per-address sources on a cache miss. That is the intended path
(REData caches the answer for everyone), but it is not free. A REData follow-up will give non-production API keys
a small share of REData's upstream spend; it is pending, and until it lands REData does not tell a development
key from production's.

## Not covered

- Calls the browser makes from the user's own address: map search through Nominatim (D25), OpenWeatherMap tile
  overlays, Esri and USGS export slides.
- What REData spends upstream on our behalf (above).
- The unheld paths in the table above.
- Weather and routing (`weather_resolution`, `routing_resolution`) still fall through from a failed REData call
  to OpenWeatherMap, Open-Meteo and OSRM. Development refuses those anyway; staging spends its 5% on them.
- Measured volumes on staging and production; the audit read only development's call log.
