# Pi Observability (#16) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. Written 2026-08-22 for a Sonnet-class agent; each task is self-contained and lands as its own commit on one branch → one PR.

**Goal:** Make the two blind spots from issue #16 visible: (1) the collector's *scattered* missed polls (~2%/day) and multi-hour outages, with a classified cause; (2) host/container health. Backup-failure alerting is **already done** (`/api/health` → UptimeRobot, artifact-age on `backup_snapshot`) and is out of scope.

**Architecture:** No new datastore. Three additive pieces:
- `pi/collector.py` writes one `collector_poll` point per loop iteration (outcome + latency + classified error). This is the Phase-3 "count the failures" item from #16.
- `pi/daily_report.py`'s existing 07:00 anomaly check gains a **data-gap check**: yesterday's raw poll coverage vs 2,880 expected, longest gap, failure breakdown from `collector_poll`. Silent unless below threshold — same contract as the category anomaly email.
- `pi/docker-compose.yml` gains one `telegraf` service (host + Docker metrics → a new `telemetry` Influx bucket, 30d retention) and a provisioned Grafana dashboard. Chosen over node-exporter+cAdvisor+Prometheus because it's one container against the datasource Grafana already has.

**Tech Stack:** Python 3 / `unittest` / `unittest.mock` (see `pi/test_weekly_report.py` for how Influx is stubbed), InfluxDB 2 / Flux, Docker Compose, Grafana provisioning.

**Closes:** GitHub issue #16 (Phases 1 & 3 fully; Phase 2 via telegraf). Housekeeping items (image pins) included; the compose-project rename is **deliberately excluded** — it orphans volumes and needs Nico at the keyboard.

---

## Global Constraints

- **UTC at rest, Pacific on display.** "Yesterday" is a Pacific calendar day; use `local_day_utc_range()` from `daily_report.py`, never `.date()` on a UTC datetime. Expected polls/day = 2,880 except DST days (2,760 / 3,000) — compute from the window length, don't hardcode.
- **Tag cardinality.** `collector_poll` tags must be a closed enum. Never tag with an error message string.
- **Silent by default.** The daily email only sends when a threshold is crossed. Don't add a "all good" line to anything.
- **No secrets in the session.** `pi/.env` is off-limits. Telegraf gets its token via `${INFLUXDB_TOKEN}` interpolation in compose, same as the other services.
- **Pi access.** `ssh nico@phrpi.local` works from the laptop. Deployed code lives at `/home/nico/SPAN`; rebuild with `cd /home/nico/SPAN/pi && git pull && docker compose up -d --build <service>`. Do **not** rebuild `influxdb` or `grafana`. Do not run `docker compose down`.
- **Tests:** `cd pi && python3 -m unittest test_weekly_report test_report_baseline test_collector_health -v` must pass before every commit. Run from `pi/`.

## File Structure

- `pi/collector.py` — **modify**: instrument the loop, write `collector_poll`.
- `pi/collector_health.py` — **new**: pure functions (error classification, gap analysis, thresholds). Keeps `collector.py` and `daily_report.py` testable without Influx.
- `pi/test_collector_health.py` — **new**: unit tests for the above.
- `pi/daily_report.py` — **modify**: add `generate_gap_check()` and call it from the loop next to `generate_anomaly_check()`; `--gap-date` CLI flag.
- `pi/test_weekly_report.py` — **modify**: add tests for the gap-email rendering.
- `pi/telegraf.conf` — **new**.
- `pi/docker-compose.yml` — **modify**: `telegraf` service; pin `grafana` and `cloudflared` tags.
- `pi/grafana/provisioning/dashboards/pi-health.json` — **new** dashboard (check the existing provisioning dir layout first; put it where the existing dashboard JSON lives).
- `pi/influx_tasks/README.md` — **modify**: document `collector_poll` and the `telemetry` bucket.
- `CLAUDE.md` — **modify**: architecture list (telegraf), Next Steps (#16 done), `status.sh` note.
- `status.sh` — **modify** (small): print yesterday's poll coverage.

---

### Task 1: `collector_health.py` — pure logic + tests

**Files:** create `pi/collector_health.py`, `pi/test_collector_health.py`.

- [ ] **Step 1: Write failing tests** in `pi/test_collector_health.py` covering:
  - `classify_error(exc) -> str` returns one of `timeout | connect | http_4xx | http_5xx | decode | other` for `httpx.TimeoutException`, `httpx.ConnectError`, `httpx.HTTPStatusError` (status 401 → `http_4xx`, 503 → `http_5xx`), `json.JSONDecodeError`/`ValueError` → `decode`, anything else → `other`.
  - `expected_polls(start_utc, stop_utc, interval_s=30) -> int` — `(stop-start)/interval`; 24h → 2880; a 23h day → 2760.
  - `gap_stats(timestamps: list[datetime], start, stop, interval_s=30) -> GapStats` with fields `present`, `expected`, `coverage` (0–1), `longest_gap_s`, `longest_gap_start` (UTC datetime or None), `gaps_over_5m` (count). A gap = consecutive-timestamp delta > `2*interval_s`; also count the lead-in gap from `start` to first timestamp and tail gap to `stop`. Test: empty list → coverage 0, longest gap = whole window; perfect 2880 → coverage 1.0, longest gap 0; one 2h50m hole → `longest_gap_s == 10200`, `gaps_over_5m == 1`.
  - `gap_alert_needed(stats, coverage_threshold=0.98, longest_gap_threshold_s=1800) -> bool` — true if coverage below threshold **or** longest gap ≥ 30 min. Test both triggers and the all-clear.
- [ ] **Step 2: Run tests, confirm they fail** (`ModuleNotFoundError`).
- [ ] **Step 3: Implement `pi/collector_health.py`.** Dataclass `GapStats`. No Influx imports — this module takes plain timestamps. Keep `httpx` import guarded only for `isinstance` checks (it's already a collector dependency; fine to import directly).
- [ ] **Step 4: Run tests, confirm pass.**
- [ ] **Step 5: Commit** — `collector_health: pure gap/coverage + error classification (#16)`.

### Task 2: Instrument `collector.py`

**Files:** modify `pi/collector.py`.

- [ ] **Step 1:** Refactor `fetch_panel_data` / `fetch_circuits` so they **raise** instead of returning `None` on failure (the catch-and-log inside them is what hides the cause today). Keep the log line.
- [ ] **Step 2:** In `collect_and_write`, wrap each fetch: record `t0`, call, on exception set `panel_err = classify_error(e)` (and likewise `circuits_err`), continue so a panel failure doesn't skip circuits. Measure `panel_ms`, `circuits_ms` as ints.
- [ ] **Step 3:** After the data write (success or failure), write a **separate** `collector_poll` point, always, best-effort (its own try/except, log-only on failure):
  ```
  collector_poll,host=phrpi,result=<ok|panel_fail|circuits_fail|both_fail|write_fail>,error=<none|timeout|connect|http_4xx|http_5xx|decode|other>
      panel_ms=<int>i,circuits_ms=<int>i,points=<int>i  <now>
  ```
  `error` = the circuits error if any, else the panel error, else `none`. `result=write_fail` when the data write raised. Use the same `now` as the data points so the two measurements line up.
- [ ] **Step 4:** Add `pi/test_collector.py`? — **No.** `collector.py` has no test harness and wiring one is out of scope; rely on Task 1's unit tests plus Step 5's live check. State this in the commit message.
- [ ] **Step 5: Deploy + verify on the Pi.** `ssh nico@phrpi.local 'cd /home/nico/SPAN/pi && git pull && docker compose up -d --build collector && sleep 90 && docker logs --tail 5 collector'`. Then query:
  ```
  from(bucket:"span") |> range(start:-5m) |> filter(fn:(r)=>r._measurement=="collector_poll") |> count()
  ```
  via the Influx UI or `docker exec influxdb influx query ...` — expect ≥2 points with `result=ok`. Do this from the feature branch **only if** the branch is what's checked out on the Pi; otherwise deploy happens after merge (Task 6) and this step verifies then. Record which you did in the task report.
- [ ] **Step 6: Commit** — `collector: emit collector_poll per iteration with classified errors (#16)`.

### Task 3: Daily data-gap check in `daily_report.py`

**Files:** modify `pi/daily_report.py`, `pi/test_weekly_report.py`.

- [ ] **Step 1: Write failing tests** (mock `query_api` as the existing tests do):
  - `render_gap_email(target_date, stats, breakdown)` → `(subject, html)`. Subject for a 2h50m outage: `⚠️ Collector missed 340 polls yesterday (11.8%) — longest gap 2h50m from 05:50`. Times **Pacific** (`America/Los_Angeles`), date from `target_date`. For a scattered-loss day with no long gap: `⚠️ Collector missed 58 polls yesterday (2.0%)` (omit the gap clause when `longest_gap_s < 300`). HTML lists: present/expected, longest gap window, and the `collector_poll` breakdown (`timeout: 40, connect: 12, …`) when non-empty, else a one-line "no collector_poll data for this day (pre-#16 deploy?)".
  - `generate_gap_check(client, target_date)` sends **nothing** when `gap_alert_needed` is false; sends once when true. Patch `send_email`.
- [ ] **Step 2: Run tests, confirm fail.**
- [ ] **Step 3: Implement.**
  - `query_poll_timestamps(query_api, start, stop) -> list[datetime]`: raw `circuit` measurement, `_field == "power_w"`, filter to **one** circuit name (pick the first tag value returned by a `distinct(column:"name")` query, or simply `|> group() |> distinct(column:"_time")` — the latter is simpler and circuit-agnostic; use it).
  - `query_poll_failures(query_api, start, stop) -> dict[str,int]`: `collector_poll` grouped by `error` tag, `count()` of `points` where `result != "ok"`.
  - `generate_gap_check(client, target_date)`: `start, stop = local_day_utc_range(target_date)`; stats via `gap_stats`; if `gap_alert_needed` → render + `send_email`. Log a one-line summary either way (`coverage 99.8%, longest gap 1m`).
  - Wire into `main()`: `--gap-date YYYY-MM-DD` on-demand flag; in `--loop`, call `generate_gap_check(client, yesterday)` right after `generate_anomaly_check`, in its own try/except.
- [ ] **Step 4: Run all `pi/` tests, confirm pass.**
- [ ] **Step 5: Dry-run against real data** (read-only, no email): `ssh nico@phrpi.local 'cd /home/nico/SPAN/pi && docker compose run --rm -e REPORT_EMAIL=nobody@invalid daily-report python daily_report.py --gap-date 2026-07-30'` — expect the log line to report ~11–12% missed and a ~2h50m longest gap (the known outage). Then `--gap-date` for yesterday → expect no alert. If `REPORT_EMAIL=nobody@invalid` would still hit Resend, instead patch via a `DRY_RUN=1` env check in `send_email` (log instead of send) — add that if it doesn't exist; it's useful permanently.
- [ ] **Step 6: Commit** — `daily_report: daily data-gap alert from raw poll coverage + collector_poll breakdown (#16)`.

### Task 4: `status.sh` coverage line

**Files:** modify `status.sh`.

- [ ] **Step 1:** Find where `status.sh` prints freshness. Add one line after it: yesterday's poll coverage (`present/expected, NN.N%`), computed with a single Flux query over `circuit` `|> group() |> distinct(column:"_time") |> count()` for the Pacific yesterday window. Reuse whatever Influx access `status.sh` already has (check how it queries freshness — don't add a new auth path). If it has none, skip this task and say so.
- [ ] **Step 2:** Run `./status.sh` from the laptop; confirm the line appears and the number is plausible (>95%).
- [ ] **Step 3: Commit** — `status.sh: show yesterday's poll coverage (#16)`.

### Task 5: Telegraf host + container metrics → Grafana

**Files:** create `pi/telegraf.conf`, `pi/grafana/provisioning/dashboards/pi-health.json`; modify `pi/docker-compose.yml`, `pi/influx_tasks/README.md`.

- [ ] **Step 1: Create the `telemetry` bucket (30d retention)** on the Pi: `docker exec influxdb influx bucket create -n telemetry -o home -r 30d` (the `influx` CLI inside the container is already authed via the setup config; if it isn't, use the UI). Record the bucket in `pi/influx_tasks/README.md`.
- [ ] **Step 2: `pi/telegraf.conf`** — inputs: `cpu`, `mem`, `disk` (mount `/` and `/hostfs`), `system` (load), `temp` (Pi SoC — needs `/sys` mounted), `docker` (per-container cpu/mem/restart via `/var/run/docker.sock`, `container_name_include=["*"]`). Output: `influxdb_v2` to `http://influxdb:8086`, org `home`, bucket `telemetry`, token `$INFLUXDB_TOKEN`. Interval 60s.
- [ ] **Step 3: Compose service:**
  ```yaml
  telegraf:
    image: telegraf:1.31
    container_name: telegraf
    restart: unless-stopped
    user: "telegraf:<docker-gid>"   # look up with `getent group docker | cut -d: -f3` on the Pi
    environment:
      - INFLUXDB_TOKEN=${INFLUXDB_TOKEN}
      - HOST_PROC=/hostfs/proc
      - HOST_SYS=/hostfs/sys
      - HOST_MOUNT_PREFIX=/hostfs
    volumes:
      - ./telegraf.conf:/etc/telegraf/telegraf.conf:ro
      - /var/run/docker.sock:/var/run/docker.sock:ro
      - /:/hostfs:ro
    depends_on: [influxdb]
  ```
- [ ] **Step 4: Pin the floating tags while here.** On the Pi: `docker image inspect grafana/grafana:latest cloudflare/cloudflared:latest --format '{{index .RepoDigests 0}} {{index .Config.Labels "org.opencontainers.image.version"}}'` to learn the *currently running* versions; pin to those exact minor tags (not the newest upstream — the point is no surprise change). TimescaleDB lives in a different compose project; leave it, note it.
- [ ] **Step 5: Deploy:** `docker compose up -d telegraf` (plus `grafana`/`cloudflared` are **not** recreated by a tag pin that matches what's running — confirm `docker compose up -d` reports them unchanged). Verify: `from(bucket:"telemetry") |> range(start:-5m) |> group(columns:["_measurement"]) |> distinct(column:"_measurement")` → expect `cpu, mem, disk, system, temp, docker, docker_container_cpu, docker_container_mem`.
- [ ] **Step 6: Grafana dashboard** `pi-health.json`, provisioned like the existing one: panels — CPU %, load, mem used %, disk free GB (`/hostfs`), SoC temp, per-container CPU, per-container mem, container restart count (stat), and a **`collector_poll` failure-rate panel** (from bucket `span`: non-ok count per hour) — this is the trend view for #16's Phase 3 question. Keep it to one screen. Manual verify at `http://phrpi.local:3000` (Safari for `.local`).
- [ ] **Step 7: Commit** — `pi: telegraf host+container metrics, Pi Health dashboard, pin image tags (#16)`.

### Task 6: Docs, deploy, close

- [ ] **Step 1:** `CLAUDE.md` — Architecture: add `telegraf` (8 services) and `collector_health.py`; Next Steps: mark #16 done with a one-line summary + the two thresholds (coverage <98% or gap ≥30 min → email); note compose-project rename remains manual. `docs/roadmap.md` Phase 1: mark #16 done.
- [ ] **Step 2:** `pi/influx_tasks/README.md` — add a short `collector_poll` schema block (tags, fields, cardinality note) and `telemetry` bucket retention.
- [ ] **Step 3:** After merge: `ssh nico@phrpi.local 'cd /home/nico/SPAN && git pull && cd pi && docker compose up -d --build collector daily-report && docker compose up -d telegraf'`. Confirm `collector_poll` points flowing (Task 2 Step 5 query) and `docker compose ps` shows all healthy.
- [ ] **Step 4:** Close #16 with a comment linking the PR and listing what was *not* done (compose rename, timescale tag pin).
- [ ] **Step 5: Commit docs** — `docs: #16 Pi observability done`.

---

## Notes for the implementer

- The backup half of #16 is already solved by `web/lib/health.ts` + UptimeRobot; don't re-add a backup line to the email.
- `rb.day_coverage_ok` (90%, on `circuit_1h` hours) gates the *category* anomaly check and is a different, coarser thing from the raw-poll coverage this plan adds. Leave it alone.
- The known 2026-07-30 outage (12:50–17:05 UTC) is the regression fixture for the gap logic — if `--gap-date 2026-07-30` doesn't light up, the check is wrong.
- Thresholds (98% / 30 min) are first guesses; put them in module constants with env overrides (`GAP_COVERAGE_MIN`, `GAP_LONGEST_MIN_S`) so they're tunable without a rebuild.
