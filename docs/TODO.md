# TODO

## Immediate: Get Panel Credentials

1. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```

2. Run registration:
   ```bash
   python span_client.py --register
   ```

3. Toggle the panel door 3 times within 30 seconds

4. Verify it worked:
   ```bash
   python span_client.py --run
   ```

5. Save the token somewhere safe (.env is git-ignored)

## Future Ideas

- Historical data logging
- Power usage alerts/thresholds
- Web UI alternative to terminal
- Export to InfluxDB/Grafana
