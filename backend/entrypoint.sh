#!/usr/bin/env sh
# Migrate, then serve. Migrations are idempotent and safe to run on every boot.
set -e

echo "sensewave: waiting for postgres..."
python - <<'PY'
import os, time, urllib.parse
import psycopg

url = os.environ["SENSEWAVE_DATABASE_URL"].replace("+asyncpg", "")
for attempt in range(60):
    try:
        with psycopg.connect(url, connect_timeout=3):
            print("sensewave: postgres is up")
            break
    except Exception as e:
        if attempt == 59:
            raise
        time.sleep(1)
PY

echo "sensewave: running migrations"
alembic upgrade head

# Idempotent: creates the admin user, and a demo site/room when asked.
if [ "${SENSEWAVE_SEED_DEMO:-true}" = "true" ]; then
  echo "sensewave: seeding (demo)"
  python -m sensewave.seed --demo
else
  echo "sensewave: seeding"
  python -m sensewave.seed
fi

echo "sensewave: starting api"
exec uvicorn sensewave.main:app --host 0.0.0.0 --port 8000 --no-access-log
