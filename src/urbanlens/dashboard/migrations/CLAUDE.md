# dashboard/migrations/ — Migration Gotchas

Applies to `src/urbanlens/dashboard/migrations/`.

- In any migration chain, index creation goes dead last (after schema/data/cleanup steps).
- For a new nullable+unique field, add `unique=True` directly in the `AddField` - the
  AddField-then-AlterField dance creates a duplicate index.
- `RenameIndex` runs immediately even when `CreateModel` indexes are deferred - beware when
  squashing.
