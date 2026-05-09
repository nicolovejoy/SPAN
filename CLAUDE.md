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
- `pi/` - Docker stack for Pi deployment (7 services)
  - `collector.py` - Polls SPAN every 30s, writes to InfluxDB
  - `bath_detector.py` - Detects bath events from heat pump signature (10min loop)
  - `charge_detector.py` - Detects EV charging sessions (10min loop)
  - `daily_report.py` - HTML email report via Resend at 7am daily
  - `rates.py` - TOU rate schedule for cost calculations
  - `docker-compose.yml` - InfluxDB, Grafana, collector, bath-detector, charge-detector, daily-report, cloudflared
  - `grafana/provisioning/` - Auto-configured datasource + dashboard

## Co-located Services

- **sentiment-arbitrage worker** — systemd timer, 3x/day weekdays. See `docs/sentiment-arbitrage.md`

## web/ — power explorer

Next.js 16 app deployed to Vercel, gated by Cloudflare Access (passkey/Face ID).
Reads InfluxDB on Pi via CF tunnel + service token.

- URL-driven state: `?range=24h&interval=1h&groupBy=category&categories=HVAC,EV`
- Auto-coarsen interval picks bucket size to stay ≤175 points across the range
- Categories sourced from `pi/categories.json` (copied to `web/categories.generated.json` by `predev`/`prebuild`)
- Deploy/auth setup: see `docs/web-deploy.md`

## Next Steps

- **Manual one-time CF + Vercel setup:** follow `docs/web-deploy.md` — expose Influx through tunnel, create service token, create two Access apps (Influx with service-auth policy; dashboard with passkey policy), import repo to Vercel with root `web/`, paste env vars
- Restart `pi/collector.py` on the Pi after pulling so new points get the `category` tag (old points read as `Other` until they age out of queries)
- **#1** cost calculations broken (PG&E placeholders → SCL TOU). Park: https://github.com/nicolovejoy/SPAN/issues/1
- Deploy sentiment-arbitrage worker to Pi: push systemd files, run setup-pi.sh, fill .env, start timer
- Add Resend DKIM/SPF DNS records + `RESEND_API_KEY`/`REPORT_EMAIL` to `pi/.env` for daily-report
- Create Grafana alert rules via UI: grid >10kW, collector down >5min, heat pump >4hr

## SPAN API

Base URL: `http://192.168.4.72/api/v1`

- `POST /auth/register` - Register client (door-proximity: toggle 3x)
- `GET /panel` - Grid power, branch data
- `GET /circuits` - Named circuits with power/energy

## Credentials

All secrets are in `pi/.env` (git-ignored). See `pi/.env.example` for the required variables.
