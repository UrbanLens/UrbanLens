# Backups

## What to back up

| What | Where | Why |
| --- | --- | --- |
| Database | `postgres-data` volume, and the dumps in the `backups` volume | Everything: accounts, pins, notes, messages. |
| Field encryption key, and `DJANGO_SECRET_KEY` | `.env` | Without `UL_FIELD_ENCRYPTION_KEY`, the encrypted columns (OAuth tokens, TOTP seeds, contact details, private notes) cannot be read. When it is unset, the key is derived from `DJANGO_SECRET_KEY`, so that secret is needed too. Losing `DJANGO_SECRET_KEY` also signs everyone out. |
| Media | `media_volume` (mounted at `src/urbanlens/media`), or your S3 bucket | Uploaded photos and documents. With the S3 backend, also the volume: it still holds `exports/`, `imports/`, `preview_sources/`, `import_previews/` and `confirmed_imports/`. |
| `.env` | The project directory | The rest of the configuration. Keep it somewhere access-controlled, because it holds every secret. |

The `backups` volume lives on the same host as the database. Copy its dumps off the host as well.

Not needed for a restore: the `clamav-db` volume (ClamAV's signature cache), `rabbitmq-data` (broker state, not database content), and Dragonfly, which has no volume. Sessions are stored in the database.

## Scheduled database backups

`celery-beat` checks hourly, at minute 2 (`run_scheduled_database_backup` in `settings/base.py`). A backup runs when the newest file is older than the configured frequency. It is a `pg_dump` plain-SQL dump, gzip-compressed, written to the `backups` volume, which is mounted at `/app/src/backups` in the `app`, `celery-worker` and `celery-worker-bulk` containers. Files beyond the retention count are deleted, oldest first.

Settings that control this:

- `UL_BACKUP_ENABLED`: the deployment's switch. When it is off, no backups run whatever the site settings say.
- `UL_BACKUP_FREQUENCY_HOURS` (default 24) and `UL_BACKUP_RETENTION` (default 30). These only seed the site settings row when it is created. Changing them later does nothing; change the live values on the site admin page.
- `UL_BACKUPS_DIR`: the directory, default `backups` under the source root.
- `UL_PG_DUMP_BIN`: the `pg_dump` binary. Compose sets it.

Site administrators can also queue a backup from the admin tools page.

## Restoring

The restore tooling runs on the host, because it uses `docker exec` and `docker cp`.

```bash
bin/restore_backup.sh --list
bin/restore_backup.sh backup_YYYYMMDD_HHMMSS.sql.gz urbanlens_restore
bin/restore_backup.sh backup_YYYYMMDD_HHMMSS.sql.gz urbanlens_restore --dry-run
```

- The first argument is a file name in the backup directory, or a path to a file on the host.
- The target database must be a new name. The script refuses the live database (`UL_DB_NAME` in the app container) and never writes to it. Pass `--drop-existing` to replace an existing scratch database.
- The restore runs as the database owner, which must be a superuser, because the dump creates extensions.
- It stops on the first error and runs in one transaction, so a failed restore leaves no partial database.
- The script finds the app container the way Compose names it, `urbanlens_<UL_CONTAINER_NAME>_app`, using `UL_CONTAINER_NAME` (else `UL_ENVIRONMENT`, else `production`) from the shell or the project's `.env`. Set `UL_APP_CONTAINER` to override it. `UL_DB_CONTAINER` defaults to the same name with `_db`.

The script creates a scratch database and does not switch the site to it. Cutting over is a separate, deliberate step. Check the result with Django:

```bash
docker exec -w /app/src/urbanlens -e UL_DB_NAME=urbanlens_restore urbanlens_production_app \
    /app/.venv/bin/python manage.py showmigrations --plan | tail -5
```

A table count does not prove the data is intact. `bin/verify_backup_restore.sh` checks that the dump-and-restore pipeline is lossless, by round-tripping the live database through scratch copies. It does not check a backup you pass it. It takes the same `UL_APP_CONTAINER` and `UL_DB_CONTAINER` variables.

Restoring the database does not restore media or `.env`. Restore the field encryption key with the database, or the encrypted columns will not decrypt.

## Rotating the field encryption key

Rotation keeps old rows readable while they are rewritten. Each step is a deploy.

1. Put the new key in `UL_FIELD_ENCRYPTION_KEY`, and move the old one into `UL_FIELD_ENCRYPTION_KEY_FALLBACKS` (comma-separated). If `UL_FIELD_ENCRYPTION_KEY` was unset, the old key is `DJANGO_SECRET_KEY`. Put that value in the fallbacks. Deploy with `docker compose up -d`.
2. Dry run, to count the rows that would change:

   ```bash
   docker compose exec -u appuser app python src/urbanlens/manage.py rotate_field_encryption --dry-run
   ```

3. Rewrite every encrypted column under the new key:

   ```bash
   docker compose exec -u appuser app python src/urbanlens/manage.py rotate_field_encryption
   ```

   The command finds every encrypted column in the project and re-encrypts each non-empty value in a transaction. If a value cannot be decrypted with any configured key, the command stops and lists up to 20 of them. Add the missing key to the fallbacks and run it again. Use `--skip-undecryptable` only when the key is gone for good: those values are already unreadable.
4. When it finishes cleanly, remove the old key from `UL_FIELD_ENCRYPTION_KEY_FALLBACKS` and deploy again.

Take a backup before step 3.

## Settings

`UL_BACKUP_*`, `UL_BACKUPS_DIR` and `UL_PG_DUMP_BIN` are described above. The full list is in [`.env-sample`](../../.env-sample).
