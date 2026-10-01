# postgis/postgis already configures the PGDG apt repo pgvector ships from -
# adding the extension is one apt-get away rather than a build from source.
FROM postgis/postgis:17-3.5

# The image is Debian 11, whose PGDG suite now lives only on apt-archive. Staying on it keeps the glibc, and so the
# collations, that existing data directories were built with.
RUN sed -i 's#http://apt.postgresql.org/pub/repos/apt/#https://apt-archive.postgresql.org/pub/repos/apt/#' /etc/apt/sources.list.d/pgdg.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends postgresql-17-pgvector \
    && rm -rf /var/lib/apt/lists/*
