# REData's news search accepts a `months` GDELT rejects

- **Status: ANSWERED 2026-10-05: fixed on REData `release/0.3.0` (`6b468d5a`, with a SearXNG news fallback in `9c127379`) and deployed to its staging. Updated 2026-10-06: both commits are in REData v0.3.4, in production since 2026-10-06 15:21Z; not re-checked against production.** Found by UrbanLens's P235 work, probing `/api/v1/search/news/` through the
  development stack. The REData code cited is `main` as of 2026-10-03.
- **Direction: outbound**, from `UrbanLens/UrbanLens` to `../REData`.
- `id: N41` · `status: current`

## What happens

`NewsSearchView` (`src/redata/api/views_location.py`) accepts `months` up to 120 and passes it to
`GdeltGateway.search` as `timespan=<months>m`. GDELT's DOC API indexes nothing before 1 January 2017 and refuses a
window reaching past it. On 2026-10-03 at 23:01Z, `q="Hudson River State Hospital"&months=120` came back as a
`GdeltError`: "GDELT returned a non-JSON body: The TIMESPAN must fall sooner than January 1, 2017."

On 2026-10-03, any `months` above 117 fails this way. The usable maximum rises by one each month.

## The ask

Clamp `months` to the whole months since 2017-01-01, not to a fixed 120. Alternatively, send GDELT a
`STARTDATETIME` no earlier than that date. A test pinned to a fixed "today" would hold the clamp.

UrbanLens does not send `months` today; its News card uses REData's default of 24.
