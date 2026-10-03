# REData's news search accepts a `months` GDELT rejects

- **Status: OPEN as of 2026-10-03.** Found by UrbanLens's P235 work, probing `/api/v1/search/news/` through the
  development stack. The REData code cited is `main` as of 2026-10-03.
- **Direction: outbound**, from `UrbanLens/UrbanLens` to `../REData`.

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
