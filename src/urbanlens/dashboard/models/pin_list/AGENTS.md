# dashboard/models/pin_list/ — Goals (from docs/GOALS.md)

## Discovery: browse and search

- Both browsing (Memories, recents, saved filters, lists) and searching should be
  independently sufficient — audit new list surfaces for whether they're reachable both ways.
- Lists backed by a saved filter must reconcile automatic (filter-driven) membership against
  manual add/remove decisions: manual decisions persist even as filter membership changes, and
  a pin present via both the filter and a manual add is never shown twice. (Known gap: no UI
  today to see or undo the manual decisions made against a filter-backed list.)
