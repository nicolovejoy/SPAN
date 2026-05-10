# SPAN power-explorer web — deploy notes

The `web/` Next.js app is **Pi-hosted** as a Docker service alongside InfluxDB
and Grafana, routed through the `phrpi` Cloudflare tunnel and gated by
Cloudflare Access (email PIN + passkey/Face ID).

> Historical note: an earlier iteration deployed to Vercel and reached Influx
> over a CF-Access-protected tunnel using a service token. We pivoted to
> Pi-hosted on 2026-05-09 — the dashboard now talks to Influx over the local
> Docker network, no service token needed at runtime. The Vercel project may
> still exist as a dormant preview target; see `CLAUDE.md` Next Steps for
> cleanup status.

## Architecture

```
browser ──(HTTPS)──▶ Cloudflare ──(phrpi tunnel)──▶ Pi:cloudflared ──▶ web:3000
                         │
                   CF Access app
                  "SPAN dashboard"
                  (email PIN + Face ID)
```

`web` (Next.js standalone) talks to `influxdb:8086` over the Docker network —
the call never leaves the Pi host. The CF-Access service-token code path in
`web/lib/influx.ts` only fires if `CF_ACCESS_CLIENT_ID/SECRET` are set, which
they aren't in the Pi compose env.

## One-time Cloudflare setup

### 1. Tunnel route

Zero Trust → Networks → Tunnels → `phrpi` → Public Hostnames, add:

- Subdomain: `span`
- Domain: `pianohouseproject.org`
- Service: `HTTP://web:3000` (the docker-compose service name)

### 2. Access app for the dashboard

Zero Trust → Access → Applications → Add → Self-hosted.

- Application name: `SPAN dashboard`
- Application domain: `span.pianohouseproject.org`
- Policy `me`: Allow, Include → Emails → `nlovejoy@me.com`
- Authentication → enable WebAuthn / passkey. First Safari visit prompts to
  enroll a Face ID passkey; afterwards Face ID is the gesture.
- MFA: configured at app level (Biometrics required). Account-level Biometrics
  MFA is also on. Session lifetime: 24h.

To share with another user: edit the `me` policy and add their email. They'll
get the same email-PIN + Face-ID enrollment flow on first visit.

### 3. (Optional) Influx external access

A separate Access app `SPAN Influx` gates `influx.pianohouseproject.org` with
a service token (`span-web` in 1Password `dev-secrets`) for any future
external client. Currently no consumer — the dashboard goes direct over the
Docker network.

## Build & deploy

The `web` service in `pi/docker-compose.yml` builds from the repo root:

```yaml
build:
  context: ..
  dockerfile: web/Dockerfile
```

Build context is the repo root so the Dockerfile can pull in
`pi/categories.json` (the single source of truth for circuit categorization)
alongside the `web/` source.

Deploy on the Pi after pulling:

```
cd ~/SPAN && git pull
cd pi && docker compose build web && docker compose up -d web
```

Only the `web` service rebuilds; Influx/Grafana/collector stay running.
Expect ~30s of dashboard downtime while the container swaps.

## Local dev

`web/.env.local` — point at the Pi over the LAN, no CF needed:

```
INFLUX_URL=http://192.168.4.72:8086    # or whatever the Pi's LAN IP is
INFLUX_TOKEN=<dev token>
INFLUX_ORG=home
INFLUX_BUCKET=span
```

(192.168.4.72 is the SPAN panel — the Pi's LAN IP differs; update as needed.)

Then:

```
cd web
npm run dev
```

`predev` copies `pi/categories.json` → `web/categories.generated.json`
automatically. Re-run `npm run sync-config` if you edit categories without
restarting the dev server.
