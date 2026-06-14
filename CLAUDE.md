# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Local observability tool for SPAN Smart Panel. Polls panel API, stores data in InfluxDB, visualizes in Grafana.

**Panel IP:** 192.168.4.72 (static)

## Quick Start

```bash
# Terminal dashboard (one-off)
./run.sh --run

# Start full stack (InfluxDB + Grafana)
cd pi && docker compose up -d

# Run collector (Mac - outside Docker due to network)
cd pi && nohup ./run_collector.sh > collector.log 2>&1 &
```

## Architecture

- `span_client.py` - CLI client with live terminal dashboard
- `pi/` - Docker stack for Pi deployment (8 services)
  - `collector.py` - Polls SPAN every 30s, writes to InfluxDB
  - `bath_detector.py` - Detects bath events from heat pump signature (10min loop)
  - `charge_detector.py` - Detects EV charging sessions (10min loop)
  - `daily_report.py` - HTML email report via Resend at 7am daily
  - `rates.py` - TOU rate schedule for cost calculations
  - `docker-compose.yml` - InfluxDB, Grafana, collector, bath-detector, charge-detector, daily-report, web, cloudflared
  - `grafana/provisioning/` - Auto-configured datasource + dashboard
- `web/` - Next.js power-explorer dashboard (Docker service, see § web/)

## Co-located Services

- **sentiment-arbitrage worker** — systemd timer, 3x/day weekdays. See `docs/sentiment-arbitrage.md`

## web/ — power explorer

Next.js 16 app, **Pi-hosted** as a Docker service alongside InfluxDB / Grafana,
routed through the same `phrpi` Cloudflare tunnel, gated by Cloudflare Access
(passkey/Face ID).

- URL-driven state: `?range=24h&interval=1h&groupBy=category&categories=HVAC,EV`
- Auto-coarsen interval picks bucket size to stay ≤175 points across the range
- Categories sourced from `pi/categories.json` (copied to `web/categories.generated.json` by `predev`/`prebuild`; the Dockerfile copies the canonical file straight into the build, no sync step needed in container)
- Talks to Influx via Docker service name (`http://influxdb:8086`) — no CF service token needed at runtime since the call never leaves the host
- Built with `output: "standalone"` for a small runtime image
- Deploy/auth setup: see `docs/web-deploy.md`

## Next Steps

- **Manifest CORS** — *Decided 2026-05-24 to live with it.* `/manifest.webmanifest` is gated by CF Access; browser fetches it without credentials and gets redirected to login → console error. The fix (separate CF Access app with Bypass policy for the four asset paths) is blocked by CF's no-overlapping-destinations rule and would require enumerating every dashboard route. Cosmetic-only — dashboard works; PWA install persists once configured. Revisit only if CF adds path-exclusion or if the noise becomes actively annoying.
- **EV monthly + annual cost rollup** in daily report (request #3 from 2026-05-23 batch — last unaddressed item). Bundle with SCL plan confirmation so cost isn't computed against two rate models.
- **Confirm SCL plan** — bill shows "Small General Energy" flat $0.1241/kWh + $0.83/day base. Check whether residential RSC tiered or TOU would be cheaper; align Grafana cost panel once decided.
- **In-email settings link** (#8) — clickable link in the daily email to change report cadence + aux-heat threshold without redeploying. Deferred 2026-06-14 (needs persistent store + web page + report-loop rework). Cadence stays daily for now.
- **Dashboard UX backlog** — web app: time range in PST, a time-nav control beyond swiping, all-axes labels (2026-06-14 feedback); polling cadence (#5), relax CF Access login (#6), 1m smoothing (#7). Custom PWA icon. Optional: de-group HVAC into per-circuit lines in the explorer (low priority).
- **HVAC cooling watch** — cooling fault found 2026-06-14 (aux resistance firing + compressor short-cycling on a hot day); turning off the HRV apparently fixed it. Confirm with a 3–6h `pi/hvac_probe.py` run during active cooling. Aux-heat alarm: Auxiliary/Heat Pump cost ≥ `$AUX_HEAT_ALARM_USD` (default $0.50/day, ≈4 kWh) → red banner + `⚠ Aux heat —` subject. Cost-based since that circuit also draws during cooling. Cold-weather suppression at #3.

## SPAN API

Base URL: `http://192.168.4.72/api/v1`

- `POST /auth/register` - Register client (door-proximity: toggle 3x)
- `GET /panel` - Grid power, branch data
- `GET /circuits` - Named circuits with power/energy

## Credentials

All secrets are in `pi/.env` (git-ignored). See `pi/.env.example` for the required variables.

<!-- SHARED-CONVENTIONS:BEGIN v=d5e16e653242 — auto-managed, do not edit here; source: prompt-lab/workflow/claude-md-shared.md (edit + re-sync) -->
## Shared conventions

<!-- These are Nico's cross-repo output rules. They're materialized into each repo's
CLAUDE.md so every agent (local, cloud, third-party) sees them as plain text. Source
of truth: prompt-lab/workflow/claude-md-shared.md — edit there and re-sync, never here. -->

- **Clickable URLs.** When pointing at any web destination (dashboard, repo, PR, deploy, settings, docs, localhost), print the full bare URL — `https://example.com` or `http://localhost:8080` — on its own, never just the page's name and never a markdown `[label](url)` link. Nico's terminal auto-linkifies raw `https://` text, so a bare URL is one-click and stays copyable.

- **Number your questions.** Any time you ask Nico more than one question, present them as a numbered list (1., 2., 3.) so he can answer by number with no ambiguity. A single standalone question needs no number.

- **Self-contained smoke-test instructions.** When you ask Nico to manually test or verify an app or website, assume zero carried-over context — he should never scroll back or recall a URL/path/credential from earlier. Always include: the exact URL (full `https://…` or `http://localhost:…`, restated even if mentioned above), the precise steps in order, and what a pass vs. fail looks like. Repetition here is a feature, not clutter.

- **No marker before a copy-paste command block.** Nico's terminal renders markdown bullets (`-`, `*`, `•`) as `●`, which breaks paste into zsh. The line directly above a fenced command block must be a plain-text label ending in a colon — never a bullet, dash, asterisk, or number. For loud copy targets, lead the label with `📋` + bold `COPY THE BELOW`, then a colon, then the block.
<!-- SHARED-CONVENTIONS:END -->
