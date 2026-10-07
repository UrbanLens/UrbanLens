# dashboard/services/wiki/ — Goals (from docs/GOALS.md)

## Wiki access

- Wikis are **not public**, despite the name. A user earns access to a location's wiki only by
  having their own pin inside that place's official boundary, potentially in addition to
  meeting other additional criteria — nothing else grants access.
- Purpose: users can only learn more about locations they already know exist. They must never
  be able to discover a location's existence through the wiki.
- Enforce by construction, everywhere: no endpoint may let a user search for, find, or see a
  wiki they haven't earned access to.
- Wiki data is versioned; the value shown for each field is resolved per-viewing-user.
  Users flagged as "concealed" have certain fields hidden from them, even on wikis they've
  otherwise earned. This must also hold by construction across every field, on every page.
