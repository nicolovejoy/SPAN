# phrpi backups

Nightly encrypted backup of everything on phrpi that git doesn't already have,
into a Cloudflare R2 bucket via [restic](https://restic.net).

## What's backed up, and why so little

The stack rebuilds from this repo — compose files, Dockerfiles, Grafana
provisioning, all the Python and TypeScript are committed. Only four things are
genuinely unrecoverable:

| What | Size (2026-07-31) | Why it matters |
|---|---|---|
| InfluxDB | ~308 MB | SPAN time-series back to 2026-01-04. Irreplaceable — you cannot re-poll the past. |
| TimescaleDB | ~71 MB | `events` hypertable (light overrides) + derived `base_prefs`. |
| Grafana volume | ~53 MB | Dashboards/users built in the UI rather than provisioned from git. |
| `.env` files | a few KB | Secrets. Git-ignored by design; without them a restore has data but no working stack. |

Total payload is under half a gigabyte. After restic deduplication, a year of
nightly snapshots should land around 1–2 GB — comfortably inside R2's 10 GB free
tier, with no egress charges on restore.

**The code tree is deliberately not backed up.** It's in git. Adding it would
inflate the repo for zero recovery value.

## One-time setup

### 1. Install restic

    sudo apt update && sudo apt install -y restic

### 2. Create the R2 bucket

Cloudflare dashboard → R2 → Create bucket, named `phrpi-backups`. Then
**Manage API tokens → Create API token**, scoped to *Object Read & Write* on
that single bucket. Don't use an account-global key: a token that can only touch
one bucket limits the blast radius if the Pi is ever compromised.

Note your Cloudflare **Account ID** from the R2 page — it goes in the repo URL.

### 3. Generate the encryption password and save it OFF the Pi

    openssl rand -base64 32

**Put this in 1Password before you do anything else.** restic encrypts
client-side, which is what makes an untrusted cloud bucket safe — but it also
means the password is the only way in. If it exists solely on the SD card and
that card dies, every backup is unrecoverable noise and this exercise achieved
nothing. This is the single most common way home backup setups fail.

### 4. Install the config

    sudo cp /home/nico/SPAN/pi/backup/restic-env.example /etc/span-backup.env
    sudo chmod 600 /etc/span-backup.env
    sudo nano /etc/span-backup.env

Fill in the account ID, both R2 keys, and the password from step 3.

### 5. Initialise the repository

    set -a; . /etc/span-backup.env; set +a
    sudo -E restic init

Run once, ever. Re-running against an existing repo is a no-op error, not
destructive.

### 6. First run, by hand

    sudo /home/nico/SPAN/pi/backup/backup.sh

Watch it through. Expect a few minutes on the first upload and well under a
minute thereafter, since only changed chunks travel.

### 7. Enable the timer

    sudo cp /home/nico/SPAN/pi/backup/span-backup.{service,timer} /etc/systemd/system/
    sudo systemctl daemon-reload
    sudo systemctl enable --now span-backup.timer
    systemctl list-timers span-backup.timer

## Routine checks

Last successful run:

    cat /var/lib/span-backup/last-success

Recent logs:

    journalctl -u span-backup.service -n 50

List snapshots:

    set -a; . /etc/span-backup.env; set +a
    sudo -E restic snapshots

Repository size:

    sudo -E restic stats --mode raw-data

## Restore runbook

**Practise this before you need it.** An untested backup is a hypothesis.

### Browse what's in a snapshot

    set -a; . /etc/span-backup.env; set +a
    sudo -E restic snapshots
    sudo -E restic ls latest

### Pull a snapshot to disk

    sudo -E restic restore latest --target /var/tmp/restore

Contents land under `/var/tmp/restore/var/tmp/span-backup.XXXXXX/`:
`influx/`, `timescale/`, `volumes/`, `env/`, `MANIFEST.txt`.

### Restore InfluxDB

Stop the writers first so nothing races the restore:

    cd /home/nico/SPAN/pi
    docker compose stop collector bath-detector charge-detector daily-report web

Then, with `<RESTORE>` being the path above:

    docker cp <RESTORE>/influx influxdb:/tmp/influx-restore
    docker exec influxdb sh -c 'influx restore /tmp/influx-restore --full -t "$DOCKER_INFLUXDB_INIT_ADMIN_TOKEN"'
    docker compose start collector bath-detector charge-detector daily-report web

`--full` replaces everything including tokens and orgs. To restore alongside
existing data instead, use `--bucket span --new-bucket span_restored` and compare
before cutting over — safer when you're recovering from corruption rather than
total loss, because it lets you diff the two.

### Restore TimescaleDB

    docker cp <RESTORE>/timescale/lights.dump timescaledb:/tmp/lights.dump
    docker exec timescaledb sh -c 'pg_restore -U "$POSTGRES_USER" -d "$POSTGRES_DB" --clean --if-exists /tmp/lights.dump'

Into a genuinely fresh cluster, load `globals.sql` first — it creates the
`lights` role that the dump's objects are owned by:

    docker cp <RESTORE>/timescale/globals.sql timescaledb:/tmp/globals.sql
    docker exec timescaledb sh -c 'psql -U "$POSTGRES_USER" -f /tmp/globals.sql'

### Restore a Docker volume

    docker run --rm -v pi_grafana-data:/data \
      -v <RESTORE>/volumes:/backup alpine:latest \
      sh -c 'rm -rf /data/* && tar xzf /backup/pi_grafana-data.tar.gz -C /data'

### Restore secrets

Files in `env/` are flattened: `SPAN_pi_.env` → `~/SPAN/pi/.env`,
`phrpi-lights_.env` → `~/phrpi-lights/.env`, `nudge_.env` → `~/nudge/.env`.

### Full rebuild on new hardware

1. Flash Raspberry Pi OS, install Docker and restic.
2. `git clone` this repo plus `phrpi-lights` and `nudge`.
3. Restore the `.env` files (above) — needs the restic password from 1Password.
4. `docker compose up -d` in each project.
5. Restore InfluxDB, TimescaleDB, and the Grafana volume as above.
6. Re-provision the Influx rollup tasks: `./pi/provision_influx_tasks.sh`.

## Design notes

- **restic, not rsync.** rsync mirrors current state — corruption propagates and
  the good copy is gone. restic keeps every snapshot, so you can go back to
  before the damage. That difference is the entire reason to use it.
- **`influx backup`, not a volume copy.** Copying TSM files out from under a
  running server risks a torn write. The supported command takes a consistent
  snapshot.
- **Fails loudly, never partially.** If a container is down the script aborts
  rather than uploading an incomplete snapshot that looks fine in `restic
  snapshots` and isn't.
- **Secrets never hit the host process table.** Tokens are dereferenced inside
  `docker exec sh -c` with single-quoted bodies, so they don't appear in `ps`.
- **Staging is wiped on every exit path.** It holds decrypted secrets and a full
  database dump; the `trap` removes it even when the script dies.

## Known gap

Failures are visible in `journalctl` and via the `last-success` file, but nothing
actively tells you. The intended fix is a line in the 7am daily report that
shouts if `last-success` is more than ~36h old — reusing the one report you
already read every morning, rather than adding another alerting channel.
Not yet implemented.
