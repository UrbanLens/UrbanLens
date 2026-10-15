# Upgrading

## 1. Back up first

Take a database backup and copy it off the host. The simplest way is the admin tools page, which queues a backup with the same code as the scheduled one. You can also copy the newest file from the `backups` volume. Copy `.env` and the media too. [Backups](backups.md) lists what to keep.

## 2. Read the changelog

Read [CHANGELOG.md](../../CHANGELOG.md) for every release between your version and the one you are installing. Entries that change the deployment say so. The 0.8.0 entry, for example, says that deploying it needs a rebuild of the database image before migrating, and two management commands afterwards. Those steps are described in the pull request the entry links to, which is not in this repository.

Before upgrading from a release earlier than 0.8.0, see [migrations from before 0.8.0](#migrations-from-before-080).

## 3. Get the new code and build

```bash
git fetch --all --tags
git checkout <release>        # a tag listed in CHANGELOG.md, or main
docker compose up -d --build
```

`--build` rebuilds every image, including `db`, which the 0.8.0 entry asks for.

## 4. Migrations

Migrations are not run by the web container. The `db-setup` service runs them on every `docker compose up`: it creates the database if it is missing, runs `migrate`, applies the per-service database logins, and exits. The `app`, `app-ws` and worker services wait for it to finish successfully.

To run the migration step on its own:

```bash
docker compose run --rm db-setup
```

Compose recreates the services that depend on `db-setup` only after it finishes, so the previous containers keep serving requests for a moment while the new schema is applied. If a release changes the schema and that matters to you, stop the web services before the upgrade with `docker compose stop app app-ws`. Then `docker compose up -d --build` starts them again after the migration.

Check the result:

```bash
docker compose ps -a db-setup       # exited with code 0
curl -fsS http://127.0.0.1:21800/health/ready    # "migrations":"current"
```

## Release-specific steps

**0.8.0.** Migration `0033_v0_8_0_indexes` refuses to run while any map overlay draws only from an external image URL, and names the fix. Before upgrading from an earlier release, run this on the running old version, which stores each such image locally and deletes nothing:

```bash
docker compose exec -u appuser app python src/urbanlens/manage.py download_overlay_image_urls
```

Then upgrade as above. A failed migration leaves `db-setup` with a non-zero exit code. Read its log with `docker compose logs db-setup`.

## Migrations from before 0.8.0

Migrations were consolidated into the 0.8.0 history. A database migrated under an earlier history records migrations that this code does not have. Django ignores a recorded migration it cannot find, so the schema could look current while it is not. The system check `dashboard.E014` refuses such a database. It runs during `migrate`.

The check's hint gives the two ways out: restore a backup taken under the current migration history, or start from a fresh database. This tree has no command that rewrites an old history.

To see whether the check fails for your database, run `docker compose run --rm db-setup` and read the error. A healthy database produces no `E014` line.
