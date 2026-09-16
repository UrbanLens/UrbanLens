# postgis/postgis already configures the PGDG apt repo pgvector ships from -
# adding the extension is one apt-get away rather than a build from source.
FROM postgis/postgis:17-3.5

RUN apt-get update \
    && apt-get install -y --no-install-recommends postgresql-17-pgvector \
    && rm -rf /var/lib/apt/lists/*
