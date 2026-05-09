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

- **🔒 Auth gap on `span.pianohouseproject.org`** (state as of 2026-05-09): dashboard loads + queries data correctly. CF Access app `SPAN dashboard` was created with policy `me` (Allow nlovejoy@me.com), but isn't actually gating the hostname because the CNAME is `proxy:off` (DNS-only) — traffic goes Mac→Vercel direct, bypassing Cloudflare. Two architectural options for next session:
  1. Flip CF proxy on (orange cloud), keep dashboard on Vercel. Risk: cert/origin chain with Vercel as origin behind CF proxy needs care.
  2. Move dashboard to Pi-hosted Docker service alongside Influx/Grafana, route through existing tunnel, gate with same CF Access pattern as Influx but human passkey instead of service token. Cleaner; loses Vercel preview deploys.
  3. Just use Vercel Authentication (Standard Protection) — kills Face-ID dream, but works today.
  Also: CF account-level MFA needs to be enabled before WebAuthn can be added (Access controls → Access settings → Allow MFA). On the in-progress app, only `onetimepin` IdP exists; WebAuthn is MFA, not primary IdP.
- **Stopgap:** before leaving the dashboard up unattended, re-enable Vercel Standard Protection on the project so it's not internet-public.
- Influx-side auth (`influx.pianohouseproject.org` → CF Access service-token policy `web service token` referencing `span-web` token) **is** working correctly — Vercel queries authenticate, anonymous gets 403.
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
