# TODO

## In Progress: Pi Deployment

Pi 5 is set up and running. Stack is deployed and working.

### What's Done
- Flashed SD card with Raspberry Pi OS Lite (64-bit) via Imager
- Configured headless setup: WiFi, SSH, hostname (`phrpi`)
- Installed Docker on the Pi
- Made GitHub repo public for easy cloning
- Cloned repo, created `.env` with SPAN token
- Started Docker stack (`docker compose up -d`)
- All containers running: InfluxDB, Grafana, collector
- Collector is successfully polling SPAN panel

### Access
Grafana works via mDNS: `http://phrpi.local:3000/d/span-main/span-panel-monitor`

Note: Use Safari (Chrome doesn't resolve `.local` domains).

### Next Steps
1. Consider ethernet to Pi for reliability (mesh WiFi can be unstable)

## Next: Move Secrets to Env Vars

Hardcoded passwords in `docker-compose.yml` are now visible in public repo:
- Grafana password
- InfluxDB password
- InfluxDB token

Move these to `.env` files (git-ignored) and reference via `${VAR}` in docker-compose.

## Future Ideas

- Power usage alerts/thresholds
- Per-circuit historical trends dashboard
- Cost calculations (integrate electricity rate)
