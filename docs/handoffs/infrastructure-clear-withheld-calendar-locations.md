# Ask: run `clear_withheld_calendar_locations` once at the 0.9.0 production rollout

- **Status: SENT 2026-10-07.** Written for Jess to pass on; this repo's sessions can't reach production.
- **Direction: outbound.** This repo to whoever owns `UrbanLens/infrastructure`'s 0.9.0 cutover (`plans/phase-9-cutover.md`).
- `id: N46` · `status: current`

Jess decided UrbanLens#301 on 2026-10-07 ([ADR-0030](../adr/0030-calendar-writes-reach-hidden-locations.md)): a Google Calendar export without auto-sync is never pushed automatically, so what
0.8.0 left on members' calendars is cleaned once, by a command, at the 0.9.0 rollout. Please add it to the cutover
steps, after migrations (0069 included) and after the new image serves.

## What it fixes

Under 0.8.0 a calendar export left a stop's address on the Google event, and the place's name in its title, after the
stop was hidden or its adder restricted who may see it (P335). 0.9.0 clears them on the next export or auto-sync push,
but an export without auto-sync is never pushed. The command rewrites, once, each event UrbanLens made that may hold a
location or title its calendar's owner may no longer see, an unscheduled stop's included. Nothing else is written: an
event with nothing withheld is skipped, an event the user deleted is not recreated, and an event an import linked from
the user's own calendar is left alone unless its fingerprint shows UrbanLens wrote what is now withheld. None written
under 0.8.0 has one, so at this rollout every such event is left alone and counted ("Left N imported events alone");
please send that count back too.

## The run

```bash
python manage.py clear_withheld_calendar_locations           # counts: "N events may hold a location or title now withheld"
python manage.py clear_withheld_calendar_locations --apply   # "Rewrote N events; M were gone from their calendars ..."
```

- Run it from a pod whose egress reaches Google (`www.googleapis.com`, `oauth2.googleapis.com`); Celery's egress policy
  allows the internet, web has none. It needs the production `UL_GOOGLE_CLIENT_ID`/`UL_GOOGLE_CLIENT_SECRET`, which the
  app pods already have.
- Every write goes through the `google_calendar` rate limiter (120 a minute site-wide, shared with members' own
  exports). When the budget runs out it waits a minute and goes on; `--no-wait` makes it stop instead. A second run
  skips every event already rewritten, so it can be interrupted and started again.
- If Google refuses the site (Calendar API disabled on the project, or the OAuth client refused) it stops with a
  `CommandError` and writes nothing more; that is the operator's to fix, and the run is repeated after.
- Please send back the counts (dry run, `--apply`, and any "Left N imported events alone"), so UrbanLens#301 and
  UrbanLens#333 can record them.
