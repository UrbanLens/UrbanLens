# dashboard/models/public_pins/ — Goals (from docs/GOALS.md)

## Public pins

- A private pin can become a "public pin" (visible without earned wiki access) only through a
  deliberately very strict, community-vote-driven bar. It's expected for this to rarely or
  never trigger in practice.
- A fresh/self-hosted deployment should have *some* way to avoid shipping with an empty map
  with no public pins — but the tooling used to seed new deployments shouldn't ship in the
  public release; other operators' deployments shouldn't be nudged to just copy our curated
  starter set. The seeding tool can be run once to seed production and then deleted (or moved
  to `../infrastructure`).
