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

## In Progress: Bath Detection

Detects bath events from heat pump circuit signature (sustained elevated draw replacing normal cycling).

### What's Done
- `pi/bath_detector.py` — detection script with `--backtest` mode
- `pi/run_bath_detector.sh` — wrapper script (points at phrpi.local InfluxDB)
- `~/Library/LaunchAgents/com.span.bath-detector.plist` — runs every 10 minutes
- Writes `bath_event` measurement to InfluxDB with duration, power stats, aux heater status

### Next Steps
1. Run backtest to validate detection against known bath times
2. Tune thresholds if needed
3. Add bath events to Grafana dashboard
4. Add Grafana annotations for bath events on the heat pump panel

## Next: Weekly Power Report

- Weekly email summarizing power consumption by circuit and time of day, with cost calculations (time-of-use rates)

## Future Ideas

- Power usage alerts/thresholds
- Per-circuit historical trends dashboard
- Cost calculations (integrate electricity rate)
