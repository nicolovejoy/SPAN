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

## Next Steps

- Redesign `span-home.json` as an **exploration dashboard** (not status snapshots): one meaningful time picker, `category` multi-select variable (HVAC/EV/Kitchen/Lights/Other) driven by existing span.json regex, stacked daily-kWh chart + breakdown that react to both. Drop all panel-level `timeFrom` overrides, the previous-30d tiles, the recirc annotation, and the dedicated heat pump panel. Discussing framing tomorrow before implementing.
- Deploy sentiment-arbitrage worker to Pi: push systemd files to repo, run setup-pi.sh, fill .env secrets, test manual run, start timer
- Add Resend DKIM/SPF DNS records to pianohouseproject.org + `RESEND_API_KEY`, `REPORT_EMAIL` to `pi/.env` for daily-report container
- Update `pi/rates.py` to match Seattle City Light (flat $0.1338/kWh, no tiers, no seasonal) — currently has PG&E defaults
- Create Grafana alert rules via UI: grid >10kW, collector down >5min, heat pump >4hr

## SPAN API

Base URL: `http://192.168.4.72/api/v1`

- `POST /auth/register` - Register client (door-proximity: toggle 3x)
- `GET /panel` - Grid power, branch data
- `GET /circuits` - Named circuits with power/energy

## Credentials

All secrets are in `pi/.env` (git-ignored). See `pi/.env.example` for the required variables.
