#!/usr/bin/env python3
"""Discover SPAN API endpoints and print their schemas."""

import json
import os
from pathlib import Path

import httpx
from dotenv import load_dotenv

PANEL_IP = "192.168.4.72"
BASE_URL = f"http://{PANEL_IP}/api/v1"

ENDPOINTS = [
    "/status",
    "/panel",
    "/storage",
    "/circuits",
    "/panel/power",
    "/panel/meter",
]

def main():
    load_dotenv(Path(__file__).parent / ".env")
    token = os.getenv("SPAN_ACCESS_TOKEN")

    if not token:
        print("No SPAN_ACCESS_TOKEN found in .env")
        return

    headers = {"Authorization": f"Bearer {token}"}

    with httpx.Client(timeout=10.0) as client:
        for endpoint in ENDPOINTS:
            print(f"\n{'='*60}")
            print(f"GET {endpoint}")
            print('='*60)

            try:
                response = client.get(f"{BASE_URL}{endpoint}", headers=headers)
                print(f"Status: {response.status_code}")

                if response.status_code == 200:
                    data = response.json()
                    print(json.dumps(data, indent=2))
                else:
                    print(f"Error: {response.text}")

            except Exception as e:
                print(f"Failed: {e}")

if __name__ == "__main__":
    main()
