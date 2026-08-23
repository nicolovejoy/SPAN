# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Local observability tool for SPAN Smart Panel. Polls panel API, stores data in InfluxDB, visualizes in Grafana.

**Panel IP:** 192.168.4.72 (static)

## Quick Start

```bash
# One-shot health check of the whole stack (panel, Pi services, freshness, backup)
./status.sh

# Terminal dashboard (one-off)
./run.sh --run

# Full stack lives on the Pi (ssh nico@phrpi.local); manage it there:
cd pi && docker compose up -d
```

## Machine roles (decided 2026-08-13)

- **Pi (`phrpi`, 192.168.5.50)** — single source of truth. Runs the whole Docker
  stack (dashboard itself is Vercel-hosted) + nightly restic backup. Stays this way deliberately: low blast radius,
  rebuilds from git.
- **Mini** (closet, ethernet to Pi, always-on) — no SPAN services. Observer/backup
  roles only (uptime monitoring is external — UptimeRobot via prompt-lab's
  declarative config; candidate second backup target). Revisit only if #18
  (TimescaleDB) lands, which would make it the storage box.
- **Laptop** — dev only. `status.sh` and `span_client.py` work from here; nothing
  runs in steady state.

## Architecture

- `span_client.py` - CLI client with live terminal dashboard
- `pi/` - Docker stack for Pi deployment (7 services)
  - `collector.py` - Polls SPAN every 30s, writes to InfluxDB
  - `bath_detector.py` - Detects bath events from heat pump signature (10min loop)
  - `charge_detector.py` - Detects EV charging sessions (10min loop)
  - `daily_report.py` - Weekly energy briefing (Mondays) + daily anomaly-check email via Resend, both at 7am
  - `rates.py` - TOU rate schedule for cost calculations
  - `docker-compose.yml` - InfluxDB, Grafana, collector, bath-detector, charge-detector, daily-report, cloudflared
  - `grafana/provisioning/` - Auto-configured datasource + dashboard
- `web/` - Next.js power-explorer dashboard (Vercel-hosted, see § web/)
- `pi/backup/` - nightly restic backup to Cloudflare R2 (systemd timer, 03:30). Covers the
  only unrecoverable state: InfluxDB, TimescaleDB, the Grafana volume, and the `.env` files —
  everything else rebuilds from git. Config at `/etc/span-backup.env` on the Pi (rendered from
  `span-backup.env.tpl` via `op inject`; the Pi has no 1Password). **Restore runbook and setup
  steps: `pi/backup/README.md`.** The repo password lives in 1Password as `phrpi-restic-backup`
  — without it every snapshot is unrecoverable ciphertext.

## web/ — power explorer

Next.js 16 app, **Vercel-hosted** (project `nico-lovejoys-projects/span`,
domain `span.pianohouseproject.org`) since 2026-08-13, auto-deployed from
GitHub pushes via the Vercel Git integration. Pi-hosted as a Docker service
2026-05-09 → 2026-08-13.

- **Client-driven state** (`ExplorerClient.tsx`, since #11 2026-06-19): a client reducer owns `{from,to,interval,show}`. Pan/zoom updates client state only — it never navigates. The URL is **intent-only** (`?range=7d&show=HVAC`), synced via `history.replaceState`; transient pan/zoom is *not* in the URL. `page.tsx` SSRs the initial breakdown + seeds the client cache for a fast first paint, then the client owns every switch. A full reload resets pan/zoom to the preset (by design).
- **Caching:** in-memory `TtlLru` (`lib/clientCache`) fronts `/api/power` + `/api/energy` (`lib/clientFetch`) → back-and-forth between visited windows = 0-network. Server LRU (`lib/queryCache`, both power + energy) + HTTP cache are the cold-miss backstop. IndexedDB-across-reload persistence still open (#10).
- **Breakdown table** = real 30s `integral()` via `/api/energy` (`cachedQueryEnergyByCategory`), keyed by window only so changing the bucket doesn't refetch it.
- Auto-coarsen interval picks bucket size to stay ≤175 points across the range
- Tests: `cd web && npm test` (vitest) — unit tests for the cache + intent-URL logic. Chart/React wiring is manual-verify.
- Categories sourced from `pi/categories.json` (copied to `web/categories.generated.json` by `predev`/`prebuild` — Vercel builds use this normal `prebuild` sync; the Dockerfile's copy-in path is a Docker-era leftover, no longer used)
- Talks to Influx via `influx.pianohouseproject.org` with the `span-web` CF Access service token (`CF_ACCESS_CLIENT_ID/SECRET` in the Vercel env activate the service-token path in `web/lib/influx.ts`)
- Built with `output: "standalone"` — a leftover from the Docker era, harmless on Vercel
- **Cloudflare Bot Fight Mode must stay OFF on the `pianohouseproject.org` zone.** It challenges the
  Vercel→Influx path and breaks the dashboard. Outage 2026-08-21: 453 of 500 firewall events in 24h
  were `bot_fight_mode` / `managed_challenge` against `influx.pianohouseproject.org` `/api/v2/query`,
  UA `influxdb-client-js`, from Vercel's AWS egress — *all* of them our own traffic, zero real bots.
  Symptom: `/api/health` 503s and charts go blank while the Pi stays perfectly healthy.
  - **A WAF skip rule does NOT fix this on the free plan.** Plain Bot Fight Mode runs ahead of WAF
    custom rules and can't be exempted; that scoping needs Super Bot Fight Mode (paid). The zone-wide
    toggle in Security → Settings is the only lever. Don't burn time writing a skip rule.
  - Nothing else protects Influx via BFM — CF Access + the service token is the real gate, and Managed
    Rules / AI Crawl Control (which do catch real scanners on other hosts in the zone) are unaffected.
  - **It presents as a rate limit, not a hard block.** Vercel egresses from a rotating AWS IP pool and
    BFM scores each IP separately, so ~1 check in 20 slips through — long down-runs punctuated by a
    single success. Both agents on the 2026-08-21 incident initially misread this as a refilling
    rate-limit budget. Firewall-events export (Security → Analytics → Events) names the rule directly;
    read it before theorising.
- `/api/health` — observer endpoint (UptimeRobot + prompt-lab's daily health email): `checks[]` of artifact ages — collector (newest raw Influx point, ≤300s) and backup (newest `backup_snapshot` point, ≤30h; written by `pi/backup/backup.sh` with the restic snapshot's own timestamp). 503 on any failure. Logic in `web/lib/health.ts`.
- Deploy/auth setup: see `docs/web-deploy.md`

## Next Steps

**Start at `docs/roadmap.md`** — phased, dependency-ordered, each phase scoped to hand to a
subagent. The list below is near-term mechanics; the roadmap explains ordering and why.

- **~~Manifest CORS~~ — resolved by the 2026-08-13 re-home to Vercel.** The premise (`/manifest.webmanifest` gated by CF Access on the Pi-hosted dashboard) no longer applies to the new topology. Previously: decided 2026-05-24 to live with the credential-less manifest fetch hitting the CF Access login redirect (cosmetic console error).
- **~~Weekly energy report + anomaly email~~ — DONE, merged 2026-08-22 (PR #19).** Manually verified
  against real InfluxDB/matplotlib on the Pi before rebuild. Weekly briefing Mondays 07:00 (headline,
  week-by-day chart, 12-week trend, usage table with Unmonitored row, HVAC block); daily anomaly check
  at 07:00, silent unless a category's median/MAD baseline is exceeded. Suppression state in
  `/app/state/anomaly_state.json` on the `report-state` volume. Retired: the nine-section daily email,
  the aux-heat alarm.
- **~~#15 `energy_wh_counter`~~ — DONE, merged 2026-08-22 (PR #20).** Both consumers
  (`pi/daily_report.py`, `web/lib/rollup.ts`) now sum `energy_wh_counter` for rollup-backed energy
  totals; `energy_wh` retained as a cross-check. Real justification (confirmed by per-circuit tracing,
  not #15's original hypothesis): the integral under-counts burst/impulse loads — chiefly the EV
  charger — that pulse faster than the 30s poll; missed-poll correlation tested at r=0.11, essentially
  none. Day-level bucketing of the counter must stay Pacific-aligned (UTC midnight = 17:00 Pacific =
  mid-EV-charging) or it manufactures spurious day-over-day swings; this mostly cancels at week/month
  scale (0.25pp / 0.14pp stdev). **Not yet done:** post-merge dashboard check — load
  https://span.pianohouseproject.org, 30d preset, confirm non-zero kWh across every category. **Dead
  code note:** Task 2's edit landed in `_circuit_kwh_flux()`/`_run_segments()` — the #9 segment router —
  which PR #19 (merged first) already made unreferenced by the shipped report. Folds into the cleanup
  candidate below rather than being a live risk.
- **#9 segment-router cleanup candidate** — `daily_report.py`'s `_run_segments` and friends,
  `query_total_kwh`, `_delta_arrow` are unreferenced by the shipped report (superseded by the weekly
  report's own query layer). Candidate for a future cleanup pass if nothing else picks them up first.
- **#16 Pi observability** (collector reliability + backup/service health) — next per
  `docs/roadmap.md` Phase 1. No plan written yet; needs a brainstorming/planning pass before
  subagent-driven execution.
- **Make bath + charge events explorable over time** — requested 2026-08-21. Their sections leave
  the email; the detectors keep writing. Probably belongs in `web/`, needs its own design.
- **Dashboard access model** — decision pending (2026-08-13); candidate: signed-cookie unlock link in Next.js middleware. /api/health (observer endpoint, see prompt-lab uptime convention) must stay exempt.
- **EV monthly + annual cost rollup** in daily report (request #3 from 2026-05-23 batch — last unaddressed item). *2026-06-15: weekly section now excludes EV (per-2h-bucket subtract) with this-week-vs-5wk + vs-12wk charts and an EV-charging-vs-weekly-avg callout; EV accounting (weekly + monthly) pinned to the exact `CHARGE_CIRCUIT` name shared with `charge_detector`, not the Car regex.*
- **~~Power explorer perf~~ (#9) — DONE, deployed 2026-07-31.** `circuit_5m`/`circuit_1h` tasks
  active, 7 months backfilled (3.77M + 314k pts), verified **-0.0032%** vs raw integral on a
  gap-free week. `web/` and `pi/daily_report.py` read them; containers rebuilt.
  - Fields: `power_w_mean`, `energy_wh` (integral), `energy_wh_counter` (SPAN's own
    meter delta). **`energy_wh_counter` is authoritative for energy since #15** — the
    integral under-counts burst loads (EV charger) faster than our 30s poll, not
    primarily a missed-poll issue as originally assumed. `energy_wh` is
    retained as a cross-check. Windows ≤48h still integrate raw; charts still read
    power. Contract: `pi/influx_tasks/README.md`.
  - Timestamps are **end-of-bucket** — a point at T covers `[T - bucket, T)`. Comparison queries
    must shift by one bucket or edges silently mismatch.
  - Tail lag: `circuit_5m` 1–6 min, `circuit_1h` 5–65 min. Consumers hybridise with raw.
  - ~~Follow-up: retune the 30d threshold~~ — done 2026-08-03, `ENERGY_5M_MAX_MS` now 7d.
  - Contract + tail-lag invariants: `pi/influx_tasks/README.md`. Read before wiring a new consumer.
- **Power explorer chart E2E** (#13) — Playwright harness via a `MOCK_INFLUX` fixture mode (hermetic local build — no live Influx dependency in CI). Locks in the 2026-06-19 pan/zoom fix: bounded-to-data, no blank-out, intent-only URL, cache hits, table-follows-zoom. Plan in the issue.
- **Zoom-in detail follow-up** — since 2026-06-19 pan/zoom stays *within* the loaded preset window (bounded by `fixLeftEdge/fixRightEdge`); zoom-in no longer auto-fetches a finer bucket. If wanted, add a deliberate "load detail at this zoom" that widens the loaded window. Low priority.
- **In-email settings link** (#8) — clickable link in the daily email to change report cadence + aux-heat threshold without redeploying. Deferred 2026-06-14 (needs persistent store + web page + report-loop rework). Cadence stays daily for now.
- **Dashboard UX backlog** — web app. Done 2026-06-19 (all in `PowerChart.tsx`): ~~time range in PST~~, ~~all-axes labels~~, ~~legend moved left~~. Done 2026-08-03: ~~per-circuit lines (#12)~~ (drill via `⌄` chip; `lib/drill.ts`), ~~time-nav beyond swiping~~ (`OverviewStrip` all-history brush + `‹ ›` step buttons), pan restored (slack window, `lib/panWindow.ts` — loaded ≈ 3× visible with silent edge extension), cost columns in the breakdown (`lib/rates.ts`, flat SCL $0.1241/kWh + $0.83/day prorated base + Δ vs previous window). Open: polling cadence (#5), ~~relax CF Access login (#6)~~ (closed 2026-08-21, superseded by the 2026-08-13 Vercel re-home — see "Dashboard access model"), 1m smoothing (#7). Custom PWA icon. **Cost model — converged.** Web costs with flat SCL ($0.1241/kWh) via `web/lib/rates.ts`; `pi/rates.py` has also used a flat SCL rate since 2026-05-15 (`is_peak()` returns `False` unconditionally), so the daily/weekly report and the web dashboard agree. (Rate-plan shopping — whether RSC tiered or TOU would be cheaper — was dropped 2026-08-21; the billed plan is flat "Small General Energy", so flat is the model to standardise on.)
- **HVAC cooling watch** — cooling fault found 2026-06-14 (aux resistance firing + compressor short-cycling on a hot day); turning off the HRV apparently fixed it. Confirm with a 3–6h `pi/hvac_probe.py` run during active cooling. ~~Aux-heat alarm~~ — retired 2026-08-22, folded into the general anomaly-detection system (the weekly report's daily anomaly check) instead of its own dedicated cost threshold/banner. Cold-weather suppression at #3.

## SPAN API

Base URL: `http://192.168.4.72/api/v1`

- `POST /auth/register` - Register client (door-proximity: toggle 3x)
- `GET /panel` - Grid power, branch data
- `GET /circuits` - Named circuits with power/energy

## Credentials

All secrets are in `pi/.env` (git-ignored). See `pi/.env.example` for the required variables.

<!-- SHARED-CONVENTIONS:BEGIN v=e5fb79b2ef4d — auto-managed, do not edit here; source: prompt-lab/workflow/claude-md-shared.md (edit + re-sync) -->
## Shared conventions

<!-- These are Nico's cross-repo output rules. They're materialized into each repo's
CLAUDE.md so every agent (local, cloud, third-party) sees them as plain text. Source
of truth: prompt-lab/workflow/claude-md-shared.md — edit there and re-sync, never here. -->

- **Clickable URLs.** When pointing at any web destination (dashboard, repo, PR, deploy, settings, docs, localhost), print the full bare URL — `https://example.com` or `http://localhost:8080` — on its own, never just the page's name and never a markdown `[label](url)` link. Nico's terminal auto-linkifies raw `https://` text, so a bare URL is one-click and stays copyable.

- **Number your questions.** Any time you ask Nico more than one question, present them as a numbered list (1., 2., 3.) so he can answer by number with no ambiguity. A single standalone question needs no number.

- **Self-contained smoke-test instructions.** When you ask Nico to manually test or verify an app or website, assume zero carried-over context — he should never scroll back or recall a URL/path/credential from earlier. Always include: the exact URL (full `https://…` or `http://localhost:…`, restated even if mentioned above), the precise steps in order, and what a pass vs. fail looks like. Repetition here is a feature, not clutter.

- **UTC at rest, Pacific on display.** Timestamps are stored in UTC, always. A *calendar day* shown to a human is `America/Los_Angeles` — Nico's day, and the clock the work actually happened on. The two rules that follow are the ones that get broken: never form a date bucket with `new Date(…).toISOString().slice(0,10)` (that is UTC, so every chart axis and "today" silently rolls over at 5pm Pacific — it put a phantom tomorrow bar on the Prompt Lab dashboard), and never bucket UTC-stamped rows with a bare `date(col)` in SQL. Use `Intl.DateTimeFormat('en-CA', { timeZone: 'America/Los_Angeles' })` in JS and an explicit zone in SQL/Python. Storage in local time is also wrong — it can't be migrated across a DST boundary without loss.

- **No marker before a copy-paste command block.** Nico's terminal renders markdown bullets (`-`, `*`, `•`) as `●`, which breaks paste into zsh. The line directly above a fenced command block must be a plain-text label ending in a colon — never a bullet, dash, asterisk, or number. For loud copy targets, lead the label with `📋` + bold `COPY THE BELOW`, then a colon, then the block.
<!-- SHARED-CONVENTIONS:END -->
