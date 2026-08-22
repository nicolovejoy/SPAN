#!/usr/bin/env bash
# Compare circuit_1h.energy_wh (integral) against energy_wh_counter (meter
# delta) per day, alongside raw poll coverage. #15.
#
# Run ON the Pi:  bash compare_energy_fields.sh
set -euo pipefail
cd ~/SPAN/pi

echo "=== daily totals: energy_wh vs energy_wh_counter ==="
docker compose exec -T influxdb influx query --org home '
from(bucket: "span")
  |> range(start: -180d)
  |> filter(fn: (r) => r._measurement == "circuit_1h")
  |> filter(fn: (r) => r._field == "energy_wh" or r._field == "energy_wh_counter")
  |> filter(fn: (r) => exists r._value)
  |> group(columns: ["_field"])
  |> aggregateWindow(every: 1d, fn: sum, createEmpty: false)
  |> pivot(rowKey: ["_time"], columnKey: ["_field"], valueColumn: "_value")
  |> map(fn: (r) => ({ r with pct_integral_over_counter:
        100.0 * (r.energy_wh - r.energy_wh_counter) / r.energy_wh_counter }))
  |> keep(columns: ["_time", "energy_wh", "energy_wh_counter", "pct_integral_over_counter"])
'

echo
echo "=== raw poll coverage per day (points across all circuits) ==="
docker compose exec -T influxdb influx query --org home '
from(bucket: "span")
  |> range(start: -180d)
  |> filter(fn: (r) => r._measurement == "circuit" and r._field == "power_w")
  |> group()
  |> aggregateWindow(every: 1d, fn: count, createEmpty: false)
'
