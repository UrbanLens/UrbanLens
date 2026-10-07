# dashboard/models/spotguessr/ — Goals (from docs/GOALS.md)

## Games

- Purpose is twofold: fun, and sourcing facts-about-locations data (with confidence ranking)
  for wikis in a way some users will do that won't manually edit a wiki.
- Store gameplay-derived data in a schema general enough to support future/different analysis
  approaches, not just whatever ML pipeline is in use today.
- Respect the same privacy defaults as everything else; anonymize/forget the contributing user
  where the data's use case doesn't require attribution.
- (Privacy model) Multiplayer games source content from the associated Wiki, never directly
  from a pin. Single-user modes are the only exception.
