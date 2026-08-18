# The frontend is built here rather than committed: `npm run build` needs node, which
# the runtime image has no other reason to carry. package files are copied on their own
# first so editing a component does not invalidate the `npm ci` layer.
FROM node:22-alpine AS ui

WORKDIR /ui

COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci

COPY frontend/ ./
RUN npm run build

FROM python:3.12-slim

WORKDIR /app

# rasterio's wheels bundle GDAL but still link against the system libexpat, which
# python-slim does not ship: importing rasterio fails with
# "libexpat.so.1: cannot open shared object file".
RUN apt-get update \
    && apt-get install -y --no-install-recommends libexpat1 \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml .
# With the `mcp` extra: the deployed API is what serves /mcp, and an image where that
# mount is silently absent - because `load_mcp_server()` found no package and returned
# None - is exactly the kind of "looks like a server problem" this project writes
# paragraphs to avoid. It also puts the `eo-rag-mcp` console script on PATH, so
# `docker run --rm -i <image> eo-rag-mcp` is a working stdio server.
RUN pip install --no-cache-dir ".[mcp]"

COPY app ./app
COPY alembic.ini .
COPY alembic ./alembic
COPY scripts/docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

# Picked up by the StaticFiles mount in app/main.py, which is conditional on this
# directory existing - so a local checkout with no build still runs.
COPY --from=ui /ui/dist ./frontend_dist

EXPOSE 8000

# The entrypoint runs `alembic upgrade head` before exec-ing whatever CMD (or
# docker-compose's `command:` override) was going to run - so `docker run` and the
# compose `--reload` dev command both get the migration for free instead of it being
# a step someone has to remember on top of either.
ENTRYPOINT ["docker-entrypoint.sh"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
