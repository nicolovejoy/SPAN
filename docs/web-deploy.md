# SPAN power-explorer web — deploy notes

The `web/` Next.js app reads from InfluxDB on the Pi over Cloudflare. Two
auth surfaces:

- **Pi-side** (Influx, machine-to-machine): Cloudflare Access **service token**.
- **Dashboard-side** (the user, browser): Cloudflare Access **passkey** (Face ID via Safari WebAuthn).

## One-time Cloudflare setup

### 1. Expose Influx through the existing tunnel

In the Cloudflare Zero Trust dashboard → Networks → Tunnels → your existing
tunnel → Public hostnames, add:

- Subdomain: `influx`
- Domain: `<your-domain>`
- Service: `HTTP://localhost:8086`

This gives you `https://influx.<your-domain>` reaching the Pi's Influx.

### 2. Create an Access service token

Zero Trust → Access → Service Auth → Service Tokens → Create.

- Name: `span-web`
- Save the Client ID and Client Secret — you'll paste them into Vercel env vars.

### 3. Create an Access application for the Influx hostname

Zero Trust → Access → Applications → Add → Self-hosted.

- Application name: `SPAN Influx`
- Application domain: `influx.<your-domain>`
- Identity providers: leave default
- Add policy:
    - Name: `web service token`
    - Action: Service Auth (not Allow!)
    - Selector: Service Token → `span-web`

That policy lets the Vercel app authenticate non-interactively with its
service-token headers, and rejects anything else.

### 4. Create an Access application for the dashboard

Zero Trust → Access → Applications → Add → Self-hosted (or "SaaS" if Vercel
serves the production hostname directly — self-hosted works either way once
the domain is on Cloudflare).

- Application name: `SPAN dashboard`
- Application domain: `dashboard.<your-domain>` (or whatever)
- Add policy:
    - Name: `me`
    - Action: Allow
    - Include: Emails → `nlovejoy@me.com`
- Authentication → enable **WebAuthn / passkey** as an authentication method.
  First visit on Safari prompts to enroll; afterwards Face ID is the gesture.

## Vercel project setup

1. New Vercel project, import the repo, set **Root Directory** to `web/`.
2. Build command auto-detected (`next build`). Install command auto-detected.
3. Environment variables (Production + Preview):
    - `INFLUX_URL=https://influx.<your-domain>`
    - `INFLUX_TOKEN=<read-only Influx token>`
    - `INFLUX_ORG=home`
    - `INFLUX_BUCKET=span`
    - `CF_ACCESS_CLIENT_ID=<from step 2>`
    - `CF_ACCESS_CLIENT_SECRET=<from step 2>`
4. Add a custom domain `dashboard.<your-domain>` and put it behind the Access app from step 4.

## Local dev

`web/.env.local` — point straight at the Pi over the LAN (no CF needed
locally):

```
INFLUX_URL=http://192.168.4.72:8086
INFLUX_TOKEN=<dev token>
INFLUX_ORG=home
INFLUX_BUCKET=span
```

Then:

```
cd web
npm run dev
```

`predev` will copy `pi/categories.json` → `web/categories.generated.json`
automatically. Re-run `npm run sync-config` if you edit categories without
restarting the dev server.
