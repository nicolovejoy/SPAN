#!/usr/bin/env bash
#
# Nightly backup of every irreplaceable byte on phrpi -> restic repo in the cloud.
#
# What's irreplaceable is short, because the stack rebuilds from git:
#   - InfluxDB   (SPAN time-series; the only truly unrecoverable dataset)
#   - TimescaleDB (lights learning-system events + derived prefs)
#   - Grafana volume (UI-created dashboards/users not in grafana/provisioning/)
#   - .env files (secrets, git-ignored by design)
#
# Everything else — compose files, Dockerfiles, provisioning, all Python/TS —
# is committed. Do NOT add the code tree here; it inflates the repo and git
# already has it.
#
# Config lives in /etc/span-backup.env (root-owned, 0600). See restic-env.example.
#
set -euo pipefail

CONFIG="${SPAN_BACKUP_CONFIG:-/etc/span-backup.env}"
STATE_DIR=/var/lib/span-backup
LOCK_FILE=/var/lock/span-backup.lock

# Retention: dense recent history, then thin out. ~24 months of monthlies keeps
# a year-over-year comparison possible, which is the whole point of this dataset.
KEEP_DAILY=14
KEEP_WEEKLY=8
KEEP_MONTHLY=24

# `restic check` reads and verifies repo structure. Too slow for every run, so
# once a week (this weekday, 1-7 with Monday=1).
CHECK_ON_WEEKDAY=7

log()  { printf '%s  %s\n' "$(date -Is)" "$*"; }
die()  { printf '%s  ERROR: %s\n' "$(date -Is)" "$*" >&2; exit 1; }

require_container() {
    local name=$1
    docker inspect -f '{{.State.Running}}' "$name" 2>/dev/null | grep -qx true \
        || die "container '$name' is not running — refusing to take a partial backup"
}

[ -r "$CONFIG" ] || die "missing config $CONFIG (copy restic-env.example, fill it in, chmod 600)"
set -a
# shellcheck source=/dev/null
. "$CONFIG"
set +a

: "${RESTIC_REPOSITORY:?not set in $CONFIG}"
: "${RESTIC_PASSWORD:?not set in $CONFIG}"

# Serialise: a slow run must never overlap the next timer firing.
exec 9>"$LOCK_FILE"
flock -n 9 || die "another backup is already running"

mkdir -p "$STATE_DIR"

STAGING="$(mktemp -d /var/tmp/span-backup.XXXXXX)"
chmod 700 "$STAGING"
cleanup() {
    # Staging holds decrypted secrets and a full DB dump — never leave it behind,
    # even on failure.
    rm -rf "$STAGING"
    docker exec influxdb rm -rf /tmp/influx-backup >/dev/null 2>&1 || true
}
trap cleanup EXIT

log "staging in $STAGING"

# --- InfluxDB -----------------------------------------------------------------
# `influx backup` is the supported path: consistent snapshot of data + metadata
# (orgs, buckets, tokens). Copying the volume out from under a running server
# would risk a torn TSM file.
#
# The token stays inside the container: passing it via `docker exec sh -c` with
# a single-quoted body means it never lands in the host's process table.
require_container influxdb
log "dumping InfluxDB"
docker exec influxdb rm -rf /tmp/influx-backup
docker exec influxdb sh -c \
    'influx backup /tmp/influx-backup -t "$DOCKER_INFLUXDB_INIT_ADMIN_TOKEN"' >/dev/null
docker cp -q influxdb:/tmp/influx-backup "$STAGING/influx" 2>/dev/null \
    || docker cp influxdb:/tmp/influx-backup "$STAGING/influx"
docker exec influxdb rm -rf /tmp/influx-backup
log "  influx: $(du -sh "$STAGING/influx" | cut -f1)"

# --- TimescaleDB --------------------------------------------------------------
# -Fc (custom format) so pg_restore can do selective restores and parallel loads.
# Globals are dumped separately: roles/passwords live in the cluster, not the DB,
# and a restore into a fresh container needs them to exist first.
require_container timescaledb
log "dumping TimescaleDB"
mkdir -p "$STAGING/timescale"
docker exec timescaledb sh -c 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc' \
    > "$STAGING/timescale/lights.dump"
docker exec timescaledb sh -c 'pg_dumpall -U "$POSTGRES_USER" --globals-only' \
    > "$STAGING/timescale/globals.sql"
[ -s "$STAGING/timescale/lights.dump" ] || die "pg_dump produced an empty file"
log "  timescale: $(du -sh "$STAGING/timescale" | cut -f1)"

# --- Docker volumes -----------------------------------------------------------
# Grafana's volume holds anything built in the UI rather than provisioned from
# git. influxdb-config is 425 B of CLI config — trivial, grabbed for completeness.
log "archiving volumes"
mkdir -p "$STAGING/volumes"
for vol in pi_grafana-data pi_influxdb-config; do
    if docker volume inspect "$vol" >/dev/null 2>&1; then
        docker run --rm \
            -v "$vol":/data:ro \
            -v "$STAGING/volumes":/backup \
            alpine:latest \
            tar czf "/backup/${vol}.tar.gz" -C /data . 2>/dev/null
        log "  $vol: $(du -sh "$STAGING/volumes/${vol}.tar.gz" | cut -f1)"
    else
        log "  WARNING: volume $vol not found, skipping"
    fi
done

# --- Secrets ------------------------------------------------------------------
# The one category git deliberately doesn't have. Without these a restore gets
# you data but no working stack.
log "collecting .env files"
mkdir -p "$STAGING/env"
found_env=0
for f in /home/nico/SPAN/pi/.env /home/nico/phrpi-lights/.env /home/nico/nudge/.env; do
    if [ -f "$f" ]; then
        # Flatten path into the filename so restores are unambiguous.
        cp "$f" "$STAGING/env/$(echo "${f#/home/nico/}" | tr '/' '_')"
        found_env=$((found_env + 1))
    fi
done
[ "$found_env" -gt 0 ] || die "no .env files found — path layout changed?"
log "  captured $found_env env file(s)"

# Manifest: what a future restore is looking at, without needing this script.
{
    echo "host:       $(hostname)"
    echo "taken:      $(date -Is)"
    echo "containers: $(docker ps --format '{{.Names}}' | sort | tr '\n' ' ')"
    echo "images:     $(docker ps --format '{{.Image}}' | sort -u | tr '\n' ' ')"
} > "$STAGING/MANIFEST.txt"

# --- restic -------------------------------------------------------------------
log "backing up to $RESTIC_REPOSITORY"
restic backup "$STAGING" \
    --tag phrpi \
    --host phrpi \
    --exclude-caches
# Staging path is a fresh mktemp dir each run, which would defeat restic's
# parent-snapshot heuristics; --host/--tag keep snapshots grouped regardless.

log "pruning old snapshots"
restic forget \
    --host phrpi \
    --keep-daily   "$KEEP_DAILY" \
    --keep-weekly  "$KEEP_WEEKLY" \
    --keep-monthly "$KEEP_MONTHLY" \
    --prune

if [ "$(date +%u)" = "$CHECK_ON_WEEKDAY" ]; then
    log "weekly integrity check"
    restic check --read-data-subset=5%
fi

date -Is > "$STATE_DIR/last-success"

# Publish the newest snapshot's own timestamp to Influx so the dashboard's
# /api/health can alarm on artifact age instead of this job's exit status — a
# success ping can lie (job ran, artifact missing); the snapshot's time can't.
# Best-effort: a failed write just lets the health check go stale, which is the
# alarm working.
publish_snapshot_time() {
    local token snap_epoch
    token=$(grep -m1 '^INFLUXDB_TOKEN=' /home/nico/SPAN/pi/.env | cut -d= -f2-) || return 1
    snap_epoch=$(restic snapshots --host phrpi --latest 1 --json | python3 -c '
import json, re, sys
from datetime import datetime
t = json.load(sys.stdin)[-1]["time"]
t = re.sub(r"\.\d+", "", t)  # fromisoformat chokes on nanosecond fractions
print(int(datetime.fromisoformat(t).timestamp()))') || return 1
    curl -sf -m 10 -X POST \
        "http://localhost:8086/api/v2/write?org=home&bucket=span&precision=s" \
        -H "Authorization: Token $token" \
        --data-binary "backup_snapshot,host=phrpi ok=1i $snap_epoch"
}
publish_snapshot_time \
    || log "WARNING: could not publish snapshot time to Influx (/api/health backup check will go stale)"

log "backup complete"
