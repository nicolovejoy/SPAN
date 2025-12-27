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
