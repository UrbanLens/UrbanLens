# dashboard/services/trips/ — Goals (from docs/GOALS.md)

## Privacy model — trip activities

- Any shared surfaces, such as trips, source content from the associated Wiki, never directly
  from a pin. Single-user modes are the only exception.
- Trip activities that reference a pin: treat trip-member sharing as explicit consent, tracked
  through the share-exposure system (`resolve_origin_share` / `record_share_exposure`). Display
  data should still come from the right place (the Wiki, or data consensually copied and stored
  separately), not a live pin reference.

## Encryption

- Trip photo archives (especially for past/completed trips) should be encrypted, ideally
  end-to-end.
