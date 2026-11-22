#!/bin/sh
# Apply DB migrations, then start the API. Several workers are safe: matchmaking uses a Redis lock
# and WebSocket delivery crosses workers via Redis pub/sub.
set -e
alembic upgrade head
exec uvicorn app.main:app --host 0.0.0.0 --port 8000 \
  --workers "${WEB_CONCURRENCY:-2}" --proxy-headers --forwarded-allow-ips='*' \
  --ws-max-size 65536 --no-server-header
