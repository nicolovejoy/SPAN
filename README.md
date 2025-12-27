# SPAN Smart Panel Monitor

Local observability tool for monitoring a SPAN Smart Panel via its REST API.

## Setup

```bash
pip install -r requirements.txt
```

## Usage

**Register with panel** (one-time, requires physical access):
```bash
python span_client.py --register
```
Toggle the panel door 3 times when prompted.

**Run live dashboard**:
```bash
python span_client.py --run
```

Displays a real-time table of circuits sorted by power consumption.

## Configuration

Panel IP is configured in `span_client.py`. Access token is stored in `.env` after registration.
