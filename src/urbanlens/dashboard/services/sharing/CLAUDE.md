# dashboard/services/sharing/ — Goals (from docs/GOALS.md)

## Privacy model (non-negotiable, applies everywhere)

- The only way pin data reaches anyone else is an explicit, opt-in, consent-tracked copy —
  never a live reference:
  - **Pin → pin**: sender shares coordinates (+ optional bundled fields) as a *suggestion*;
    recipient must accept; on accept it becomes the recipient's own independent pin. The
    recipient never gets access to the sender's actual pin, before or after acceptance.
  - **Pin → wiki**: sender opts in per field; the wiki gets a copy. The wiki never has read
    access to the source pin or any data inside it.
- Every REST endpoint and every query must enforce this **by construction**.
- Trip activities that reference a pin: treat trip-member sharing as explicit consent, tracked
  through this service (`resolve_origin_share` / `record_share_exposure`). Display data should
  still come from the right place (the Wiki, or data consensually copied and stored
  separately), not a live pin reference.
