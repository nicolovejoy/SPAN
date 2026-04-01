# Sentiment Arbitrage Worker — Pi Deployment

Co-located on the SPAN Raspberry Pi. Runs a Python pipeline 3x/day (weekdays, 9am/1pm/6pm ET) that pulls Reddit posts, scores sentiment with FinBERT, fetches stock prices, and writes to Firestore. Powers the dashboard at ryan.ibuild4you.com.

**Repo:** https://github.com/nicolovejoy/sentiment-arbitrage — see `worker/` for setup, systemd units, and config.

**Pi user:** `nico` (not `pi`)

## Why the Pi

Reddit blocks datacenter IPs (Railway) with 403s. The Pi's residential IP works with Reddit's public JSON endpoints.

## Resource Impact

- **At rest:** zero — no long-running process
- **During runs (~2min, 3x/day):** ~500MB–1GB RAM spike (torch + FinBERT inference on ~100 posts), then fully released
- **Disk:** ~2GB (venv with torch/transformers + cached FinBERT model ~400MB)
- No conflict with SPAN stack (collector, detectors are idle lightweight loops)
