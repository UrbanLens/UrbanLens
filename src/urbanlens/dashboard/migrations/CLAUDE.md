- In any migration chain, index creation goes dead last
- For a new nullable+unique field, add `unique=True` directly in the `AddField`. 
  AddField-then-AlterField creates a duplicate index.
- `RenameIndex` runs immediately even when `CreateModel` indexes are deferred
