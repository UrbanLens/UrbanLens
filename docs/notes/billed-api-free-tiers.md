# Billed API free tiers

> **Written by a Claude agent. Not authoritative.**
>
> This records what one automated session measured or believed on the date
> below. It was not independently reviewed, its numbers may be stale, and the
> code may have moved. Re-run the measurement before relying on it, and
> **rewrite this file** when you do — do not add a correction underneath the
> old claim. When this file and the code disagree, the code wins.

## R31 — Each billed API is held to a monthly ceiling, so every deployment together stays inside the vendor's free tier

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
- **Share**: `UL_BILLED_API_SHARE`, between 0 and 1. When unset it follows `UL_ENVIRONMENT`:

  | `UL_ENVIRONMENT` | Default share |
  |---|---|
  | `production` | 0.8 |
  | `staging` | 0.1 |
  | `development` | 0.05 |
  | `local` | 0.05 |
  | anything else | 0 |

  `0` keeps a deployment off every billed API. The test suite takes 1.0. A development box spends
  only with `UL_ALLOW_OUTBOUND_APIS=true`, and then only its 0.05.

### UrbanLens's billed services

| Service | Billed as | Free a month | Allotment | Production / staging / dev |
|---|---|---|---|---|
| `google_geocoding` | Geocoding, plus Place Details Essentials for the legacy `cid:` lookup | 10,000 | 0.4 | 3,200 / 400 / 200 |
| `google_places` | Nearby Search (Enterprise fields), legacy Place Details, Place Photos, legacy Autocomplete; held to the smallest free SKU | 1,000 | 0.4 | 320 / 40 / 20 |
| `google_maps` | Static Maps and Street View Static (REData calls neither) | 10,000 | 0.9 | 7,200 / 900 / 450 |
| `azure_maps` | Search | 5,000 | 0.4 | 1,600 / 200 / 100 |

Not held here:

- Google Street View metadata costs nothing.
- OpenWeatherMap's `data/2.5` plan refuses past its limit rather than billing.
- The browser's Mapbox token spends map loads directly, and nothing server-side can count them.
- The AI providers (OpenAI, Anthropic, Cloudflare Workers AI) have no free tier to hold them to.
  The assistant and several other AI paths write no `ApiCallLog` row, so their spend is not
  visible here either. R20 lists them for a decision.

### Changing it

Set `UL_BILLED_API_SHARE` per deployment, and keep the shares of every deployment at or below 1.
To change the cross-repo split, change `free_tier_allotment` here and REData's together, so they
sum to 0.9 or less.
