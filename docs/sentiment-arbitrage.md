# Sentiment Arbitrage Worker — Pi Deployment

Co-located on the SPAN Raspberry Pi. Runs a Python pipeline 3x/day that pulls Reddit posts, scores sentiment with FinBERT, fetches stock prices, and writes to Firestore. Powers the dashboard at ryan.ibuild4you.com.

**Repo:** https://github.com/nicolovejoy/sentiment-arbitrage (worker/ directory)

## Why the Pi

Reddit blocks datacenter IPs (Railway) with 403s. The Pi's residential IP works with Reddit's public JSON endpoints.

## Schedule

9am, 1pm, 6pm US Eastern, weekdays only. Implemented as a systemd timer at fixed UTC times (13:00, 17:00, 22:00 UTC = EDT). During EST (Nov–Mar), runs fire 1hr early ET.

## Resource Impact

- **At rest:** zero — no long-running process
- **During runs (~2min, 3x/day):** ~500MB–1GB RAM spike (torch + FinBERT inference on ~100 posts), then fully released
- **Disk:** ~2GB (venv with torch/transformers + cached FinBERT model ~400MB)
- No conflict with SPAN stack (collector, detectors are idle lightweight loops)

## Files on Pi

```
/home/pi/sentiment-arbitrage/
  worker/
    main.py              # entry point
    requirements.txt     # torch (CPU), transformers, finnhub-python, firebase-admin
    .env                 # secrets (not in git)
    venv/                # Python 3.11+ virtualenv
    sentiment-worker.service   # systemd oneshot unit
    sentiment-worker.timer     # systemd timer unit
    setup-pi.sh          # one-shot setup script
```

## Secrets (.env)

Three required variables — edit `/home/pi/sentiment-arbitrage/worker/.env`:

- `FINNHUB_API_KEY` — stock price API
- `GOOGLE_APPLICATION_CREDENTIALS_JSON` — Firebase service account JSON (inline, one line)
- `FIRESTORE_PROJECT_ID` — set to `sentiment-arbitrage`

## Setup

SSH to the Pi, then either SCP the files or push them to the repo first:

```bash
# Option A: SCP from Mac
scp /tmp/sentiment-arbitrage/worker/{setup-pi.sh,sentiment-worker.service,sentiment-worker.timer} pi@<PI_IP>:~/

# Option B: If files are pushed to repo, just run:
curl -sL https://raw.githubusercontent.com/nicolovejoy/sentiment-arbitrage/main/worker/setup-pi.sh | bash
```

The setup script handles: clone repo, create venv, install deps, download FinBERT model (~400MB), create .env template, install systemd units.

## Post-Setup

Fill in secrets:
```bash
nano /home/pi/sentiment-arbitrage/worker/.env
```

Test manually:
```bash
sudo systemctl start sentiment-worker
journalctl -u sentiment-worker -f
```

Start the timer:
```bash
sudo systemctl start sentiment-worker.timer
```

Verify timer is active:
```bash
systemctl list-timers sentiment-worker.timer
```

## Verification

A successful run logs:
- Posts fetched from r/investing, r/stocks, r/wallstreetbets
- FinBERT sentiment scores
- Firestore writes to `sentiment_scores`, `sentiment_posts`, `price_snapshots`

A failed run shows `HTTP Error 403` (Reddit blocking) or Firestore auth errors.

## Systemd Commands

```bash
# Logs
journalctl -u sentiment-worker --since today

# Timer status
systemctl status sentiment-worker.timer

# Disable
sudo systemctl stop sentiment-worker.timer
sudo systemctl disable sentiment-worker.timer
```
