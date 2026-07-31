#!/usr/bin/env python3
"""Rebuild the circuit_5m / circuit_1h rollup measurements from raw `circuit` data.

The rollups are a pure speed optimisation for the power explorer (#9) — raw
`circuit` is never deleted, so this script can always rebuild them from scratch.

The aggregation pipeline is NOT duplicated here: this script reads the same
.flux files the live Influx tasks run (pi/influx_tasks/*.flux), strips their
task header (the `option task` block and its schedule-derived time bounds) and
substitutes explicit chunk bounds. Points produced by a backfill are therefore
byte-for-byte identical to those the live task would have produced for the same
window — same measurement, tags, fields, and end-of-bucket timestamps.

Aggregation runs server-side via Flux `to()`; no sample data crosses the wire.

Examples:
    python backfill_rollups.py --dry-run
    python backfill_rollups.py --measurement circuit_5m --from 2026-07-01 --to 2026-07-08
    python backfill_rollups.py                      # full history, both measurements
"""

import argparse
import logging
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from influxdb_client import InfluxDBClient

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

INFLUXDB_URL = os.getenv("INFLUXDB_URL", "http://localhost:8086")
INFLUXDB_TOKEN = os.getenv("INFLUXDB_TOKEN")
INFLUXDB_ORG = os.getenv("INFLUXDB_ORG", "home")
INFLUXDB_BUCKET = os.getenv("INFLUXDB_BUCKET", "span")

TASK_DIR = Path(__file__).parent / "influx_tasks"

# Raw collection started 2026-01-04.
DATA_START = datetime(2026, 1, 4, tzinfo=timezone.utc)

HEADER_BEGIN = "// --- BEGIN TASK HEADER ---"
HEADER_END = "// --- END TASK HEADER ---"

# Per-measurement chunking. Chunk size trades query memory/latency on the Pi
# against per-chunk overhead. Both were measured against the live Pi (2026-07-31,
# 21 circuits at a 30s sample cadence):
#
#   circuit_5m, 1 day  ->  ~60k raw points in,  18,144 points out,  ~2s
#   circuit_1h, 7 days -> ~423k raw points in,  10,584 points out,  ~12s
#
# Flux streams windows, so peak memory tracks the raw read, not the range: a
# 7-day read is ~420k float points (~20-30 MB of table buffers) which the Pi
# handles comfortably, while a naive whole-history query would be ~13M points.
# 5m is chunked tighter than 1h purely because it emits ~12x more points per
# unit time, so its write batches are the binding constraint, not the read.
# Lower with --chunk-days if the Pi is under load.
CHUNK_DAYS = {"circuit_5m": 1, "circuit_1h": 7}

# Bucket width per measurement — chunk boundaries are snapped to this so no
# bucket ever straddles two chunks (which would make the pipeline emit a
# truncated bucket at each chunk edge).
BUCKET_SECONDS = {"circuit_5m": 300, "circuit_1h": 3600}

# Must match `bucketEvery` / `bucketPeriod` in the corresponding .flux file.
FLUX_BUCKET = {
    "circuit_5m": ("5m", "5m30s"),
    "circuit_1h": ("1h", "1h30s"),
}

MEASUREMENTS = list(CHUNK_DAYS)


def load_pipeline(measurement: str) -> str:
    """Read a task .flux file and strip its task header, leaving the pipeline body."""
    path = TASK_DIR / f"{measurement}.flux"
    text = path.read_text()

    if HEADER_BEGIN not in text or HEADER_END not in text:
        raise SystemExit(f"{path}: missing task header markers — cannot reuse pipeline")

    body = text.split(HEADER_END, 1)[1]
    if "|> to(" not in body:
        raise SystemExit(f"{path}: no to() sink found in pipeline body")
    return body.strip("\n")


def build_header(measurement: str, start: datetime, stop: datetime) -> str:
    """Backfill equivalent of the task header, with explicit chunk bounds.

    Emits buckets whose END stamp falls in (start, stop] — i.e. every bucket
    fully covering [start, stop). Mirrors the task header exactly: read one
    bucket earlier (window() clips at the range edge and emits a bogus partial
    whose stamp lands on `emitFrom`, which the body filters out) and one sample
    interval later (so the last bucket's trapezoid reaches its right edge).
    """
    every, period = FLUX_BUCKET[measurement]
    width = timedelta(seconds=BUCKET_SECONDS[measurement])
    return f'''import "date"

srcBucket = "{INFLUXDB_BUCKET}"
srcMeasurement = "circuit"
dstBucket = "{INFLUXDB_BUCKET}"
dstOrg = "{INFLUXDB_ORG}"
dstMeasurement = "{measurement}"

bucketEvery = {every}
bucketPeriod = {period}
tailSlack = 30s

emitFrom = {iso(start)}
emitTo = {iso(stop)}
readFrom = {iso(start - width)}
readTo = date.add(d: tailSlack, to: emitTo)
'''


def iso(t: datetime) -> str:
    return t.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# Replaces the to() sink in --dry-run, and is appended after it on a real run,
# so both modes report how many points were (or would be) written per field
# without streaming the rows themselves back to the client.
COUNT_TAIL = '|> group(columns: ["_field"])\n    |> count()'


def build_flux(measurement: str, body: str, start: datetime, stop: datetime,
               dry_run: bool) -> str:
    if dry_run:
        body = re.sub(r'\|> to\([^)]*\)', COUNT_TAIL, body)
    else:
        body = body.rstrip() + "\n    " + COUNT_TAIL
    return build_header(measurement, start, stop) + "\n" + body


def snap(t: datetime, seconds: int, up: bool = False) -> datetime:
    """Snap a time down (or up) to a bucket boundary."""
    epoch = int(t.timestamp())
    floor = epoch - epoch % seconds
    if up and floor != epoch:
        floor += seconds
    return datetime.fromtimestamp(floor, tz=timezone.utc)


def run_chunk(query_api, flux: str) -> int:
    """Execute one chunk; return the number of points written (or projected)."""
    total = 0
    for table in query_api.query(flux, org=INFLUXDB_ORG):
        for record in table.records:
            total += record.get_value()
    return total


def backfill(client: InfluxDBClient, measurement: str, start: datetime,
             stop: datetime, chunk_days: int, dry_run: bool) -> None:
    width = BUCKET_SECONDS[measurement]
    start = snap(start, width)
    stop = snap(stop, width)
    if stop <= start:
        logger.warning(f"{measurement}: empty range after snapping to bucket bounds, skipping")
        return

    body = load_pipeline(measurement)
    query_api = client.query_api()
    step = timedelta(days=chunk_days)

    chunks = []
    cursor = start
    while cursor < stop:
        chunks.append((cursor, min(cursor + step, stop)))
        cursor += step

    verb = "Would write" if dry_run else "Wrote"
    logger.info(
        f"{measurement}: {iso(start)} -> {iso(stop)} "
        f"({len(chunks)} chunk(s) of {chunk_days}d){' [DRY RUN]' if dry_run else ''}"
    )

    grand_total = 0
    t0 = time.monotonic()
    for i, (c_start, c_stop) in enumerate(chunks, 1):
        flux = build_flux(measurement, body, c_start, c_stop, dry_run)
        c0 = time.monotonic()
        try:
            n = run_chunk(query_api, flux)
        except Exception as e:
            logger.error(f"{measurement} [{i}/{len(chunks)}] {iso(c_start)}: FAILED: {e}")
            continue
        grand_total += n
        elapsed = time.monotonic() - c0
        done = time.monotonic() - t0
        eta = done / i * (len(chunks) - i)
        logger.info(
            f"{measurement} [{i}/{len(chunks)}] {iso(c_start)} -> {iso(c_stop)}  "
            f"{verb.lower()} {n:>7,} pts in {elapsed:5.1f}s  (eta {eta / 60:.1f}m)"
        )

    logger.info(
        f"{measurement}: done — {verb.lower()} {grand_total:,} points "
        f"in {(time.monotonic() - t0) / 60:.1f}m"
    )


def parse_time(s: str) -> datetime:
    for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    raise argparse.ArgumentTypeError(f"unrecognised time: {s!r} (use YYYY-MM-DD)")


def main():
    parser = argparse.ArgumentParser(
        description="Rebuild circuit_5m / circuit_1h rollups from raw circuit data",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--from", dest="start", type=parse_time, default=DATA_START,
                        help="start (UTC, YYYY-MM-DD), default 2026-01-04 (start of data)")
    parser.add_argument("--to", dest="stop", type=parse_time, default=None,
                        help="end (UTC, YYYY-MM-DD), default now")
    parser.add_argument("--measurement", choices=MEASUREMENTS + ["all"], default="all",
                        help="which rollup to rebuild (default: all)")
    parser.add_argument("--chunk-days", type=int, default=None,
                        help=f"override chunk size (defaults: {CHUNK_DAYS})")
    parser.add_argument("--dry-run", action="store_true",
                        help="report how many points would be written, write nothing")
    args = parser.parse_args()

    if not INFLUXDB_TOKEN:
        logger.error("INFLUXDB_TOKEN not set")
        return 1

    stop = args.stop or datetime.now(timezone.utc)
    if stop <= args.start:
        logger.error("--to must be after --from")
        return 1

    targets = MEASUREMENTS if args.measurement == "all" else [args.measurement]

    logger.info(f"InfluxDB={INFLUXDB_URL} org={INFLUXDB_ORG} bucket={INFLUXDB_BUCKET}")

    # Wide chunks can take minutes on the Pi; the client default (10s) is far too low.
    client = InfluxDBClient(
        url=INFLUXDB_URL, token=INFLUXDB_TOKEN, org=INFLUXDB_ORG, timeout=900_000
    )
    try:
        for m in targets:
            backfill(client, m, args.start, stop,
                     args.chunk_days or CHUNK_DAYS[m], args.dry_run)
    finally:
        client.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
