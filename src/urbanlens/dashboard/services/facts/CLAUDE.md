# dashboard/services/facts/ — Goals (from docs/GOALS.md)

## Games

- Games exist partly to source facts-about-locations data (with confidence ranking) for wikis,
  in a way some users will do that won't manually edit a wiki — this service is the consumer of
  that gameplay-derived data.
- Store gameplay-derived data in a schema general enough to support future/different analysis
  approaches, not just whatever ML/confidence-engine pipeline (e.g. the current Facts
  confidence engine) is in use today.
- Respect the same privacy defaults as everything else; anonymize/forget the contributing user
  where the data's use case doesn't require attribution.
