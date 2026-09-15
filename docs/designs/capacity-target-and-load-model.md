# D15 — The site is built for 1,000 concurrent signed-in users now and 10,000 later, and "concurrent" means a person browsing with 30 seconds between pages

> **Written by a Claude agent. Not authoritative.**
>
> This records what one automated session measured or believed on the date
> below. It was not independently reviewed, its numbers may be stale, and the
> code may have moved. Re-run the measurement before relying on it, and
> **rewrite this file** when you do — do not add a correction underneath the
> old claim. When this file and the code disagree, the code wins.

`id: D15` · `status: accepted` · `updated: 2026-09-15`

> **The targets are Jess's; the load model and the budgets are an agent's
> proposal.** `accepted` records the targets. The think time, the journey
> weights and the millisecond ceilings below are a first model built from the
> templates, and want replacing with real traffic the moment there is any.

## The target

Jess, 2026-09-15:

> We must be able to support 1000 concurrent users now, and 10k concurrent
> users eventually, so major adjustments are going to have to be made to make
> that possible.

D11 decides that one user's expensive request must not reach another's. This
decides how many users the whole site must serve while that holds. They are
different failures: the neighbour test (X15) can pass on a site that falls over
at 200 people, and a capacity run can pass on a site where one person's import
still stalls everybody.

## What one concurrent user is

A number of users means nothing until it says what each one does. A thousand
open tabs nobody touches cost a thousand idle sockets; a thousand people
hammering the filter cost something else entirely. The model the harness
(`tests/perf/k6/population.js`) runs:

- **A page view, then time on the page.** Think time is log-normal with a
  30-second median and σ = 0.8, clamped to 3-600 s. The mean is about 41 s.
- **What a page view is**, weighted: map 30, pin 14, home 8, notifications 6,
  messages 6, conversation 5, wiki 5, organize 5, search dialog 5, memories 4,
  trips 4, a friend's profile 4, own profile 3, a pin's visit tab 1.
- **What a signed-in page brings with it**: the header's unread counts and
  safety banner render in the page's own request
  (`partials/layout/header.html`), so a page view is the page plus whatever
  that page fetches as it loads.
- **What the map does**, read from `pages/map/index.html`: the whole pin
  document when the browser has no pin cache (30% of users on their first map
  visit), the meta otherwise; 40% of map visits then type into the name filter,
  one debounced search per keystroke group, three of them. Half of those
  filter sessions have the pin list open, which refetches the list once the
  filter commits (`_refreshPinList`), sent without the viewport's `bounds`, so
  it counts the account's whole match set. A quarter of map visits type a
  pin's name into the search box, one `map.autocomplete.local` request per
  400 ms pause, three of them. Both shares are assumptions, not observations;
  `UL_CAP_SIDEBAR_SHARE` and `UL_CAP_SEARCH_BOX_SHARE` change them.
- **What an open page keeps doing**: unread messages every 60 s, and the map's
  meta every 2 minutes.
- **The notification socket** that `_notification_push.html` opens on every
  page and closes on the next navigation, with its 45 s heartbeat. So the
  handshake rate is the page-view rate, not a one-off.
- **Each user arrives from their own address**, as they would through the
  tunnel, so a per-visitor proxy limit sees a thousand visitors.

The population behind it (`provision_integration_env --population`) is sized
like a real site rather than like a fixture: 60% of accounts hold 10-100 pins,
30% 100-1,000, 9% 1,000-5,000 and 1% 10,000-20,000, each with three labels a
pin, friends, conversations with unread messages, notifications and visits.

### What that implies, by arithmetic rather than measurement

About 2.4 requests per page view (the page, what it fetches alongside, the bell
for one view in ten, and the polls a long view runs) and a 41-second cycle give
roughly **24 page views, ~60 application requests and ~24 socket handshakes a
second, with 1,000 sockets open**, at 1,000 users. At 10,000 it is ten times
each: ~600 requests a second and 10,000 open sockets. Those are the numbers a design has to be
checked against before anything is measured.

## What "supports" means

At each hold, including the ones below the target, so a failure says at what
level the site broke:

| | ceiling |
|---|---|
| a full page, p95 | < 1,000 ms |
| a fragment, poll or JSON call, p95 | < 500 ms |
| requests failed | < 0.5% |
| notification socket handshakes that open | > 99.5% |
| the Postgres pool | never above 80% of `max_connections` (the readiness endpoint's figure) |
| sessions still signed in | 100% |

The map document is recorded but not judged: its cost is the account's size,
and the 1% of accounts at 20,000 pins would set its p95 by themselves. X17 and
D12 own that number.

## What this does not cover yet

- **Writes.** Nobody imports, uploads, edits a label or sends a message. A
  capacity figure without writes is a ceiling on reading.
- The messages page's own socket, games, media, trips' editing, the external
  API, map tiles (served by third parties, except the proxied REData layers).
- The search box on the pages other than the map that carry it: the composer
  in `themes/base.html`, trips, lists, the photo vault and the safety map.
- Saved-filter counts are left out because nothing requests them: the toolbar
  asks only for filters with no icon, and every write path gives one (X20).
- **The host.** The perf environment runs on chiron with the load generator
  beside it, not on damballa. A figure from it says where the design breaks,
  not what production will do on the day.
- Growth of the data behind the users: a thousand users a year in hold far more
  than the population seeds.
