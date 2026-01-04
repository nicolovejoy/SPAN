# TODO

## In Progress: Pi Deployment

Pi 5 is set up and running. Stack is deployed but there's a network connectivity issue.

### What's Done
- Flashed SD card with Raspberry Pi OS Lite (64-bit) via Imager
- Configured headless setup: WiFi, SSH, hostname (`phrpi`)
- Installed Docker on the Pi
- Made GitHub repo public for easy cloning
- Cloned repo, created `.env` with SPAN token
- Started Docker stack (`docker compose up -d`)
- All containers running: InfluxDB, Grafana, collector
- Collector is successfully polling SPAN panel

### Current Issue: Network Connectivity
Mac cannot reach Pi's Grafana (port 3000) despite:
- SSH working fine between Mac and Pi
- Grafana responding on Pi localhost
- Docker listening on 0.0.0.0:3000
- No firewall blocking

Symptoms:
- `curl http://192.168.4.142:3000` fails from Mac
- Ping from Mac to Pi times out
- Pi can ping Mac successfully
- Asymmetric routing - likely Eero mesh issue

Pi details:
- Hostname: `phrpi`
- IP: `192.168.4.142`
- WiFi: "Piano House" (5GHz, signal -64 dBm)

### Next Steps
1. Reboot Mac to clear network state, retry `curl http://192.168.4.142:3000`
2. If still failing, check Eero app for client isolation settings
3. Consider running ethernet to Pi for reliability (mesh WiFi is unstable)
4. Once Grafana accessible, verify dashboard shows SPAN data
5. Stop collector on Mac once Pi collector confirmed working

## Future Ideas

- Power usage alerts/thresholds
- Per-circuit historical trends dashboard
- Cost calculations (integrate electricity rate)
