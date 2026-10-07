# dashboard/models/pin/ — Goals (from docs/GOALS.md)

## Privacy model (non-negotiable, applies everywhere)

- A pin and everything inside it is private by default. There is no setting that makes a pin
  itself visible or searchable to another user. Full stop.
- The only way pin data reaches anyone else is an explicit, opt-in, consent-tracked copy —
  never a live reference (see `pin_share/` and `services/sharing/` for the mechanics).
- Every REST endpoint and every query must enforce this **by construction** — it must be
  structurally impossible for a bug (human or agent-written) to leak a private pin's live
  data through some other surface, not just conventionally discouraged.
