#!/bin/sh
# Runs migrations before the API starts, so the schema is never a manual step someone
# forgets after `git pull`. Idempotent either way (see alembic/versions/): a volume
# still on the old scripts/init_db.sql schema upgrades in place, a brand new one gets
# built fresh, and re-running against an up-to-date database is a no-op.
set -e

alembic upgrade head
exec "$@"
