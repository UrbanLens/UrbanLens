# Billed API free tiers

> **Written by a Claude agent. Not authoritative.**
>
> This records what one automated session measured or believed on the date
> below. It was not independently reviewed, its numbers may be stale, and the
> code may have moved. Re-run the measurement before relying on it, and
> **rewrite this file** when you do — do not add a correction underneath the
> old claim. When this file and the code disagree, the code wins.

## R31 — Each billed API is held to a monthly ceiling at this deployment's share, so every deployment together stays inside the vendor's free tier

`id: R31` · `status: current` · `updated: 2026-10-05`

The cross-repo table lives in REData's `docs/BILLED_APIS.md` (R20 there). It covers every billed
API either repo calls, its free tier, which deployments hold a key, and the budgets before and
after. This record is UrbanLens's half.

### How a ceiling is set

A vendor's free allowance belongs to the billing account, and all of these draw on it at once:
UrbanLens production, staging and development, plus REData. So every call to a billed service
must fit, per calendar month, inside:

```
vendor free tier × UrbanLens allotment × this deployment's share     (rounded down)
```

`check_rate_limit` (`dashboard/services/core/rate_limiter.py`) refuses a call past that whatever
the admin-editable `ApiRateLimit` row says. The pieces are:

- **Vendor free tier**: `ServiceDefaults.free_tier_per_calendar_month`.
- **Allotment**: `ServiceDefaults.free_tier_allotment`.
  - At most 0.4 of a Google Maps Platform or Azure SKU that REData can also bill. REData takes 0.5.
  - Never more than 0.9. The rest is headroom: Google's month turns at midnight Pacific, while
    `ApiCallLog.this_calendar_month` counts UTC months.
- **Share**: `UL_ENVIRONMENT_SHARE`, between 0 and 1, which replaced `UL_BILLED_API_SHARE` (D26). One service
  can be given its own with `UL_ENVIRONMENT_SHARE_OVERRIDES` (`google_geocoding=0.01`). When unset it follows
  `UL_ENVIRONMENT`:

  | `UL_ENVIRONMENT` | Default share |
  |---|---|
  | `production` | 0.9 |
  | `staging` | 0.05 |
  | `development` | 0 |
  | `local` | 0 |
  | anything else | 0 |

  `0` keeps a deployment off every billed API: development is refused outright
  (`EnvironmentRefusedError`, no `ApiCallLog` row) unless an override opts one service in. The test suite takes
  1.0. The same share scales every `quota` and `billed` service's per-minute, per-day and 30-day windows, not
  only a billed one's monthly ceiling (`egress.service_share`, `rate_limiter.check_rate_limit`).

### UrbanLens's billed services

| Service | Billed as | Free a month | Allotment | Production / staging / dev |
|---|---|---|---|---|
| `google_geocoding` | Geocoding, plus Place Details Essentials for the legacy `cid:` lookup | 10,000 | 0.4 | 3,600 / 200 / 0 |
| `google_places` | Nearby Search (Enterprise fields), legacy Place Details, Place Photos, legacy Autocomplete; held to the smallest free SKU | 1,000 | 0.4 | 360 / 20 / 0 |
| `google_maps` | Static Maps and Street View Static (REData calls neither) | 10,000 | 0.9 | 8,100 / 450 / 0 |
| `azure_maps` | Search | 5,000 | 0.4 | 1,800 / 100 / 0 |

Off production, a REData failure never falls through to direct Google (D26): the place-name chain stops after
REData, so the 441 background Google Geocoding calls a week development made when production REData answered
503 cannot recur from staging either.

`protomaps_basemap` (the hosted basemap, 900,000 a 30 days) is production's alone: elsewhere
`UL_PROTOMAPS_API_KEY` is ignored and the self-hosted mirror serves the layers.

Not held here:

- Google Street View metadata costs nothing.
- OpenWeatherMap's `data/2.5` plan refuses past its limit rather than billing.
- The browser's Mapbox token spends map loads directly, and nothing server-side can count them.
- The AI providers (OpenAI, Anthropic, Cloudflare Workers AI) have no free tier to hold them to.
  They are logged, not held: see "AI calls" below.

### AI calls

AI runs on Cloudflare Workers Paid through a very cheap model, and about $10 a month is accepted. So
no spend ceiling holds an AI call, and nothing here adds one: a call is logged, never refused for spend.
That is Jess's decision; spend is read from the log, not enforced. Every environment may call AI (D26).
Each feature reserves an `api_call_slot` under its own name, so its `ApiRateLimit` switch can turn it
off; the six rows added for D26 carry only a per-minute guard against a runaway loop.

Every AI call writes one `ApiCallLog` row through `rate_limiter.log_api_call`, or fills in the
reserved row where the caller already holds an `api_call_slot` (so there is never a second one):

| Column | Holds |
|---|---|
| `service` | The feature, as in the table below; `ai_<provider>` for a gateway built without one |
| `model` | The model id the provider names, e.g. `@cf/meta/llama-3.1-8b-instruct` |
| `endpoint` | `provider:model`, so the provider reads off the row |
| `created`, `response_ms` | When the call was made, and how long it took |
| `success`, `status_code` | Whether the provider answered; 502 when `ai-inference` reports the provider call failed, otherwise empty |
| `input_tokens`, `output_tokens` | What the provider reported; empty when it reports none (a classifier, some Cloudflare models) |
| `cost_estimate` | This call's tokens at the model's price in `MODEL_COSTS`; empty for a model with no price on file |

No row holds a prompt, an image or an answer. A call refused before it was made for an input that
could not return data keeps its `was_rejected_input` row, and a call the gateway never sent (a prompt
over the token limit) writes none.

| Feature | `service` | Row written by |
|---|---|---|
| Assistant, one row per provider round | `assistant` | the reserved row (D26), filled in by `LLMGateway` |
| Link extraction | `link_extraction` | the reserved row (D26), filled in by `LLMGateway` |
| Document pin import | `document_pin_import` | the reserved row (D26), filled in by `LLMGateway` |
| Category suggestions | `category_suggestions` | the reserved row (D26), filled in by `LLMGateway` |
| Label style suggestions | `label_style_suggestions` | the reserved row (D26), filled in by `LLMGateway` |
| Trip suggestions | `trip_suggestions` | the reserved row (D26), filled in by `LLMGateway` |
| Trivia generation | `trivia_generation` | the reserved row (D26), filled in by `LLMGateway` |
| Trivia moderation, answer check and wiki incorporation; article expansion and safety | the feature's own name | the reserved row, filled in by `LLMGateway`; each is also held by its `ApiRateLimit` |
| Photo keywords (vision) | `ai_photo_keywords` | the reserved row, filled in by `vision.py` |
| Photo classifier | `cloudflare_image_classifier` | the reserved row, filled in by `vision.py` |
| Ollama photo keywords (self-hosted) | `ollama` | the gateway session, with `rate_limiter.annotate_calls` |

`LLMGateway` writes the row in `_get_response`, so a provider added to it is logged without further
code. `ai-inference` has no database and writes its own request line to stdout only. Not measured:
what a month of these rows adds up to.

VirusTotal's public API bars commercial use. UrbanLens is noncommercial, so its terms are met.

### Changing it

Set `UL_ENVIRONMENT_SHARE` per deployment only to depart from the defaults, and keep the shares of every
deployment at or below 1.
To change the cross-repo split, change `free_tier_allotment` here and REData's together, so they
sum to 0.9 or less.
