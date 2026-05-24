import { NextResponse } from "next/server";
import { queryPower } from "@/lib/influx";
import { INTERVAL_ORDER, intervalSeconds, type IntervalKey } from "@/lib/interval";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

const isInterval = (v: string): v is IntervalKey =>
  (INTERVAL_ORDER as string[]).includes(v);

export async function GET(request: Request) {
  const url = new URL(request.url);
  const fromMs = Number(url.searchParams.get("from"));
  const toMs = Number(url.searchParams.get("to"));
  const intervalRaw = url.searchParams.get("interval") ?? "1h";

  if (!Number.isFinite(fromMs) || !Number.isFinite(toMs) || fromMs >= toMs) {
    return NextResponse.json(
      { error: "invalid from/to (ms epoch, from < to required)" },
      { status: 400 },
    );
  }
  if (!isInterval(intervalRaw)) {
    return NextResponse.json({ error: `invalid interval ${intervalRaw}` }, { status: 400 });
  }

  // Defensive quantize so unquantized clients still produce stable cache keys.
  const intervalMs = intervalSeconds(intervalRaw) * 1000;
  const qFromMs = Math.floor(fromMs / intervalMs) * intervalMs;
  const qToMs = Math.floor(toMs / intervalMs) * intervalMs;

  const data = await queryPower({ fromMs: qFromMs, toMs: qToMs, interval: intervalRaw });

  // Trailing-bucket queries (to ≈ now) change as time passes; cap cache at
  // one bucket. Historical queries (to is well in the past) are immutable.
  const isTrailing = Date.now() - toMs < 2 * intervalMs;
  const maxAge = isTrailing ? Math.max(60, Math.floor(intervalMs / 1000)) : 86400;
  return NextResponse.json(
    { data },
    {
      headers: {
        "Cache-Control": `public, max-age=${maxAge}, stale-while-revalidate=3600`,
      },
    },
  );
}
