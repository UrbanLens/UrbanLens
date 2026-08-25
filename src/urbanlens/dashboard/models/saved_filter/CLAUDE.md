# dashboard/models/saved_filter/ — Goals (from docs/GOALS.md)

## Discovery: browse and search

- Both browsing and searching should be independently sufficient — audit new saved-filter
  surfaces for whether they're reachable both ways.
- Saved filters apply near-instantly on the map — cached/indexed such that toggling one is not
  a perceptible round trip.
- Lists backed by a saved filter must reconcile automatic (filter-driven) membership against
  manual add/remove decisions: manual decisions persist even as filter membership changes, and
  a pin present via both the filter and a manual add is never shown twice. (Known gap: no UI
  today to see or undo the manual decisions made against a filter-backed list.)
