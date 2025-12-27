#!/usr/bin/env python3
"""SPAN Smart Panel local monitoring client."""

import argparse
import os
import sys
import time
from pathlib import Path

import httpx
from dotenv import load_dotenv
from rich.console import Console
from rich.live import Live
from rich.table import Table

PANEL_IP = "192.168.4.72"
BASE_URL = f"http://{PANEL_IP}/api/v1"
ENV_FILE = Path(__file__).parent / ".env"

console = Console()


def load_token() -> str | None:
    """Load access token from .env file."""
    load_dotenv(ENV_FILE)
    return os.getenv("SPAN_ACCESS_TOKEN")


def save_token(token: str) -> None:
    """Save access token to .env file."""
    env_content = f"SPAN_ACCESS_TOKEN={token}\n"
    ENV_FILE.write_text(env_content)
    console.print(f"[green]Token saved to {ENV_FILE}[/green]")


def register() -> None:
    """Register with the SPAN panel using door-proximity auth."""
    console.print("[yellow]Starting registration...[/yellow]")
    console.print("[bold]Please toggle your door 3 times now![/bold]")

    payload = {
        "name": "ClaudeVibeMonitor",
        "description": "Local monitoring via Claude Code"
    }

    try:
        with httpx.Client(timeout=30.0) as client:
            response = client.post(f"{BASE_URL}/auth/register", json=payload)
            response.raise_for_status()

            data = response.json()
            token = data.get("accessToken")

            if token:
                save_token(token)
                console.print("[green]Registration successful![/green]")
            else:
                console.print("[red]No accessToken in response[/red]")
                console.print(f"Response: {data}")

    except httpx.HTTPStatusError as e:
        console.print(f"[red]HTTP error: {e.response.status_code}[/red]")
        console.print(f"Response: {e.response.text}")
    except httpx.RequestError as e:
        console.print(f"[red]Request failed: {e}[/red]")


def build_circuits_table(circuits: list[dict]) -> Table:
    """Build a rich table from circuit data."""
    table = Table(title="SPAN Circuits - Live Dashboard")

    table.add_column("Name", style="cyan", no_wrap=True)
    table.add_column("Power (W)", justify="right", style="magenta")
    table.add_column("Status", style="green")

    # Sort by power (highest first)
    sorted_circuits = sorted(
        circuits,
        key=lambda c: abs(c.get("instantPowerW", 0)),
        reverse=True
    )

    for circuit in sorted_circuits:
        name = circuit.get("name", "Unknown")
        power = circuit.get("instantPowerW", 0)
        relay_state = circuit.get("relayState", "UNKNOWN")

        # Color power based on value
        if abs(power) > 1000:
            power_str = f"[bold red]{power:.1f}[/bold red]"
        elif abs(power) > 100:
            power_str = f"[yellow]{power:.1f}[/yellow]"
        else:
            power_str = f"{power:.1f}"

        # Color status
        if relay_state == "CLOSED":
            status_str = "[green]ON[/green]"
        elif relay_state == "OPEN":
            status_str = "[dim]OFF[/dim]"
        else:
            status_str = relay_state

        table.add_row(name, power_str, status_str)

    return table


def fetch_circuits(client: httpx.Client, token: str) -> list[dict]:
    """Fetch circuits from the SPAN API."""
    headers = {"Authorization": f"Bearer {token}"}
    response = client.get(f"{BASE_URL}/circuits", headers=headers)
    response.raise_for_status()

    data = response.json()
    # API returns {"spaces": {...}, "circuits": {...}}
    circuits_dict = data.get("circuits", {})
    return list(circuits_dict.values())


def run_dashboard() -> None:
    """Run the live dashboard."""
    token = load_token()

    if not token:
        console.print("[red]No access token found. Run with --register first.[/red]")
        sys.exit(1)

    console.print("[green]Starting live dashboard (Ctrl+C to exit)...[/green]")

    try:
        with httpx.Client(timeout=10.0) as client:
            with Live(console=console, refresh_per_second=1) as live:
                while True:
                    try:
                        circuits = fetch_circuits(client, token)
                        table = build_circuits_table(circuits)
                        live.update(table)
                    except httpx.HTTPStatusError as e:
                        console.print(f"[red]API error: {e.response.status_code}[/red]")
                    except httpx.RequestError as e:
                        console.print(f"[red]Connection error: {e}[/red]")

                    time.sleep(2)

    except KeyboardInterrupt:
        console.print("\n[yellow]Dashboard stopped.[/yellow]")


def main() -> None:
    """Main entry point."""
    parser = argparse.ArgumentParser(description="SPAN Smart Panel Monitor")
    parser.add_argument("--register", action="store_true", help="Register with the panel")
    parser.add_argument("--run", action="store_true", help="Start the live dashboard")

    args = parser.parse_args()

    if args.register:
        register()
    elif args.run:
        run_dashboard()
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
