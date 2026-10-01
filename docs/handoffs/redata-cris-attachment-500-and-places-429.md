# Two REData production failures UrbanLens hit on 2026-10-01: some CRIS attachment downloads 500, and nearby Places answers 503

- **Status: OPEN as of 2026-10-01.** Found by UrbanLens's location integration suite on a v0.8.0 dev environment
  (`v080e2e`); reproduced directly against `https://redata.urbanlens.org` with UrbanLens's API key, no UrbanLens
  code in the path.
- **Direction: outbound**, from `UrbanLens/UrbanLens` to `../REData`.

## 1. `GET /api/v1/cultural-resources/<uuid>/attachments/<id>/download/` returns 500 for some attachments

Reproducible on every request: a `text/html` "Something went wrong - REData" page, 1,157 bytes, no request ID header.
The resource itself (`GET /api/v1/cultural-resources/<uuid>/`) answers 200 and lists the attachment.

| Resource | Attachment | Listed as | Download |
|---|---|---|---|
| `54d64e82-04d4-438c-8003-056aa3480754` (Hudson River State Hospital) | `480502` | `application/pdf`, "From Inventory Form" | **500** |
| `54d64e82-04d4-438c-8003-056aa3480754` | `480501` | `application/pdf`, "Building Inventory Form" | **500** |
| `029e1eb4-f03d-401e-a00b-b0087b720158` | `480473` | `application/pdf` | **500** |
| `160ea13e-6985-4f37-b11d-9ed91105249e` | `479722` | PDF | 200 |
| `9e555257-038d-46fd-9854-d15ecfe136fa` | `479929` | PDF | 200 |
| `85a3b922-4d39-4397-9c24-d798ada395ed` | `480077` | PDF | 200 |
| `7ced81cc-0d16-4c48-a2dd-7389e8755265` | `480257` | PDF | 200 |
| `c8513f93-8c3d-447d-971b-0f3d938d23c0` | `480824` | PDF | 200 |

So it is not every PDF, and not every attachment on one resource works either. The sample is the first attachment
of six resources from `lookup_cultural_resources(41.73328, -73.92812, radius_meters=600, provider="ny_cris")`, plus
both attachments of the HRSH resource.

What UrbanLens does with it: `PinCrisAttachmentView` maps the failure to a 404, and the CRIS card's thumbnail
falls back to an icon. Nothing breaks, but the inventory forms for the most-visited campus never show.

**Asked:** the cause from REData's logs, and whether an attachment REData cannot serve should be a JSON 4xx/5xx
with `error`/`message` like the rest of the API, rather than the HTML error page. UrbanLens reads the JSON body to
tell "not available" from "try later".

## 2. `GET /api/v1/places/search/nearby/` answers 503 because Google answered 429

```
503 {"error":"places_api_unavailable","message":"Places API (New) answered 429 for https://places.googleapis.com/v1/places:searchNearby."}
```

Earlier the same day it answered `503 {"error":"rate_limited","message":"Places API (New) request budget is
exhausted ..."}`. UrbanLens opens its breaker for 60 s on either. Every new pin's naming and wiki enrichment asks
this endpoint first, so while it holds, pins get worse names.

**Asked:** whether REData's Google Places quota or budget is the limit being hit, and whether UrbanLens's key
should be sharing it. The intent recorded on UrbanLens's side is that production REData is effectively unlimited
for UrbanLens, and that a throttle is a dev or staging key question. The dev environment above used
`UL_REDATA_API_KEY`.

## Not asked

UrbanLens needs no change for either. Both already degrade to an icon or a fallback name.
