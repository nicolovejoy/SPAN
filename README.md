# SPAN Smart Panel Monitor

Local observability tool for monitoring a SPAN Smart Panel via its REST API.

## Setup

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

## Usage

Use the `run.sh` wrapper (handles venv automatically):

```bash
./run.sh --register   # one-time, toggle panel door 3x
./run.sh --run        # start live dashboard
```

Or manually activate venv first:
```bash
source venv/bin/activate
python span_client.py --run
```

Displays a real-time table of circuits sorted by power consumption.

## Configuration

- **Panel IP:** `192.168.4.72` (static IP configured on router)
- **Access token:** Stored in `.env` after registration (git-ignored)

## Pi Deployment

The `pi/` directory contains a Docker stack for continuous monitoring on a Raspberry Pi.

```bash
ssh nico@phrpi.local          # connect to Pi
cd SPAN/pi && docker compose up -d   # start stack
```

Grafana: `http://phrpi.local:3000` (admin / spanmonitor123)

**Note:** If Grafana won't load in Chrome, try Safari.
