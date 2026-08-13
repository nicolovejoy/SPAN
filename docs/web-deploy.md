# SPAN power-explorer web — deploy notes

The `web/` Next.js app is **Vercel-hosted** (project `nico-lovejoys-projects/span`,
production domain `span.pianohouseproject.org`). It reads InfluxDB on the Pi over
the CF-Access-protected hostname `influx.pianohouseproject.org` using the
`span-web` service token.

> Historical note: the app was Pi-hosted (Docker service `span-web` behind the
> `phrpi` Cloudflare tunnel) from 2026-05-09 to 2026-08-13, when it was re-homed
> to Vercel. An even earlier iteration was also Vercel-hosted — this is a return
> to that shape. The Pi `web` service and the tunnel's `span` public-hostname
> route were retired 2026-08-13.

## Architecture

```
browser ──(HTTPS)──▶ Vercel (span.pianohouseproject.org)
                        │
                        ▼  CF Access service token (span-web)
                influx.pianohouseproject.org
                        │  (phrpi Cloudflare tunnel)
                        ▼
                  Pi: influxdb:8086
```

- `web/lib/influx.ts` attaches the service-token headers whenever
  `CF_ACCESS_CLIENT_ID` / `CF_ACCESS_CLIENT_SECRET` are set — they are in the
  Vercel env, so every Influx call goes through the CF Access app gating
  `influx.pianohouseproject.org`.
- Grafana and Influx hostnames still ride the `phrpi` tunnel; only the
  dashboard itself moved off the Pi.

## Vercel setup

- Project: `nico-lovejoys-projects/span`, linked to the public GitHub repo via
  the Vercel Git integration — pushes auto-deploy (production on the default
  branch, previews otherwise).
- Domain: `span.pianohouseproject.org` — DNS CNAME to Vercel.
- Environment variables (Preview + Production):
  - `INFLUX_URL` — `https://influx.pianohouseproject.org`
  - `INFLUX_ORG` / `INFLUX_BUCKET` / `INFLUX_TOKEN`
  - `CF_ACCESS_CLIENT_ID` / `CF_ACCESS_CLIENT_SECRET` — the `span-web` service
    token (stored in 1Password `dev-secrets`)

## Deploy

Normal path — just push; the Git integration deploys automatically:

```
git push
```

Manual production deploy, from `web/`:

```
cd web && vercel deploy --prod
```

Vercel builds run the normal `prebuild` categories sync
(`pi/categories.json` → `web/categories.generated.json`); the Docker-era
`web/Dockerfile` path is no longer used.

## Cloudflare leftovers (optional tidying)

The old CF-tunnel public-hostname route for `span` (Zero Trust → `phrpi`
tunnel) is unused — DNS no longer points at it — and the "SPAN dashboard"
Access app has nothing behind it. Removing both is optional manual cleanup in
the CF dashboard. The `SPAN Influx` Access app stays: it is what the `span-web`
service token authenticates against.

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
