# dashboard/migrations/ — Migration Gotchas

Applies to `src/urbanlens/dashboard/migrations/`.

- In any migration chain, index creation goes dead last (after schema/data/cleanup steps).
- For a new nullable+unique field, add `unique=True` directly in the `AddField` - the
  AddField-then-AlterField dance creates a duplicate index.
- `RenameIndex` runs immediately even when `CreateModel` indexes are deferred - beware when
  squashing.
- A named `Index`'s name must fit in 30 characters (Postgres's identifier limit; Django's
  `models.E034` catches it) - easy to blow past with this project's `idxdb_<model>_<fields>`
  convention on a model with a long name or several fields. `manage.py check` catches it (a
  migration can be generated and committed with an over-length name with no error until this
  runs); `makemigrations` does not. **Neither does the pytest suite** - pytest-django's test
  database setup applies the migration directly without Django's system checks, so a full green
  test run does not prove a new migration is deploy-safe. Run `manage.py check` (and ideally
  `manage.py migrate` against a real dev database) for any migration-touching change, not just
  the test suite.
