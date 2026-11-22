#!/usr/bin/env bash
# Nightly backup of the Friends database and uploaded photos (run on the VPS from the repo folder).
#
#   ./scripts/backup.sh                  # writes backups/friends-YYYYmmdd-HHMM.{sql.gz,media.tar.gz}
#   KEEP_DAYS=14 ./scripts/backup.sh     # keep two weeks (default 7)
#
# Cron (daily 03:30):  30 3 * * * cd /opt/friends && ./scripts/backup.sh >> backups/backup.log 2>&1
#
# Restore the database:
#   gunzip -c backups/friends-XXXX.sql.gz | docker compose exec -T postgres psql -U friends -d friends
# Restore photos:
#   docker compose run --rm -v "$PWD/backups:/b" --entrypoint tar api -xzf /b/friends-XXXX.media.tar.gz -C /data/media
#
# Copy the backups folder off the server too (e.g. rclone to cloud storage) — a backup on the
# same disk won't survive losing the VPS.
set -euo pipefail
cd "$(dirname "$0")/.."

KEEP_DAYS="${KEEP_DAYS:-7}"
OUT="backups"
STAMP="$(date +%Y%m%d-%H%M)"
mkdir -p "$OUT"
chmod 700 "$OUT"

echo "[$(date -Is)] backing up database"
docker compose exec -T postgres pg_dump -U friends -d friends --no-owner --clean --if-exists \
  | gzip -9 > "$OUT/friends-$STAMP.sql.gz.part"
mv "$OUT/friends-$STAMP.sql.gz.part" "$OUT/friends-$STAMP.sql.gz"

echo "[$(date -Is)] backing up media"
docker compose run --rm -T -v "$PWD/$OUT:/b" --entrypoint tar api -czf "/b/friends-$STAMP.media.tar.gz" -C /data/media .

find "$OUT" -name 'friends-*.gz' -mtime +"$KEEP_DAYS" -delete
echo "[$(date -Is)] done: $(ls -1 "$OUT" | grep -c "friends-$STAMP") files, $(du -sh "$OUT" | cut -f1) total"
