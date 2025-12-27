# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Local observability tool for monitoring a SPAN Smart Panel. Connects to the panel's local REST API to display real-time circuit data.

**Panel IP:** 192.168.4.72

## Commands

```bash
# Install dependencies
pip install -r requirements.txt

# Register with panel (requires door-proximity auth - toggle door 3x)
python span_client.py --register

# Run live dashboard
python span_client.py --run
```

## Architecture

- `span_client.py` - Single-file client with CLI interface
- `.env` - Stores `SPAN_ACCESS_TOKEN` after registration (git-ignored)

## SPAN API

Base URL: `http://192.168.4.72/api/v1`

- `POST /auth/register` - Register client, returns accessToken (requires `door-bypass: true` for door-proximity auth)
- `GET /circuits` - Returns circuit data (requires Bearer token)

Circuit data structure includes `instantPowerW`, `relayState`, and `name` fields.
