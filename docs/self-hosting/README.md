# Self-hosting UrbanLens

Pages in this directory:

- [Install](install.md): from a clone to a running site, with the first admin and email.
- [Configuration](configuration.md): how settings are read, the groups of settings, outbound calls, API keys, storage, plugins and page overrides.
- [Upgrading](upgrading.md): backing up, migrating, and the migration-history check.
- [Backups](backups.md): what to back up, restoring, and rotating the field encryption key.
- [Operations](operations.md): health endpoints, metrics, logs, workers, scheduled tasks, ClamAV and the external API.

Plugins are documented in [docs/plugins/](../plugins/README.md).

## Architecture

`docker-compose.yml` runs a gunicorn web process (`app`) and a Daphne WebSocket server (`app-ws`) behind an nginx front end (`nginx`, host port 21800), with a second nginx (`media-nginx`, port 21801) for uploads when you serve media from its own host. Celery runs as several worker services plus `celery-beat`; `celery-metrics` is opt-in.

State is in PostgreSQL 17 with PostGIS (`db`), three Dragonfly instances (Redis-compatible: cache, tile cache and channel layer), RabbitMQ as the Celery broker, and a ClamAV daemon (`clamav`) that scans uploads before they are stored.
