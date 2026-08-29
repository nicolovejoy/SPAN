import { NextResponse } from "next/server";
import { cachedQueryEnergyByCategory } from "@/lib/queryCache";
import { buildEnergyRows, comparisonGrain, snapPeriod } from "@/lib/energyWindow";
import { isCategory } from "@/lib/categories";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

export async function GET(request: Request) {
  const url = new URL(request.url);
  const fromMs = Number(url.searchParams.get("from"));
  const toMs = Number(url.searchParams.get("to"));
  // Optional: rows for this category's individual circuits instead of the five
  // category rows (#12). The client fetches both and nests one under the other,
  // which keeps the category response cached across a drill toggle.
  const drill = url.searchParams.get("drill") ?? undefined;

  if (!Number.isFinite(fromMs) || !Number.isFinite(toMs) || fromMs >= toMs) {
    return NextResponse.json(
      { error: "invalid from/to (ms epoch, from < to required)" },
      { status: 400 },
    );
  }
  if (drill !== undefined && !isCategory(drill)) {
    return NextResponse.json({ error: `invalid drill ${drill}` }, { status: 400 });
  }

  // The table no longer describes the viewed window: every column describes
  // the Pacific calendar period (day/week/month/year) that window snaps to,
  // compared against the prior period.
  const grain = comparisonGrain(toMs - fromMs);
  const nowMs = Date.now();
  const snap = snapPeriod(toMs, grain, nowMs);
  const [current, prevPeriod] = await Promise.all([
    cachedQueryEnergyByCategory({ fromMs: snap.fromMs, toMs: snap.toMs, drill }),
    cachedQueryEnergyByCategory({ ...snap.previous, drill }),
  ]);
  const data = buildEnergyRows(current, prevPeriod, {
    periodFromMs: snap.fromMs,
    periodToMs: snap.toMs,
    periodGrain: grain,
    periodComplete: snap.complete,
  });

  // Trailing window changes as time passes; historical is immutable. Keys off
  // the snapped current period's toMs — a partial period's toMs ≈ now.
  const isTrailing = Date.now() - snap.toMs < 2 * 60_000;
  const maxAge = isTrailing ? 60 : 86400;
  return NextResponse.json(
    { data },
    {
      headers: {
        "Cache-Control": `public, max-age=${maxAge}, stale-while-revalidate=3600`,
      },
    },
  );
}
