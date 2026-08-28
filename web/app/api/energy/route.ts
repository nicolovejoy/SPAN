import { NextResponse } from "next/server";
import { cachedQueryEnergyByCategory } from "@/lib/queryCache";
import { buildEnergyRows, comparisonGrain, paceRanges } from "@/lib/energyWindow";
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

  // Δ column compares calendar periods (Pacific), not the viewed window: the
  // period containing the window's end, so far, vs the same elapsed span of
  // the prior period. Two extra windows through the same cache.
  const pace = paceRanges(toMs, comparisonGrain(toMs - fromMs));
  const [current, period, prevPeriod] = await Promise.all([
    cachedQueryEnergyByCategory({ fromMs, toMs, drill }),
    cachedQueryEnergyByCategory({ ...pace.current, drill }),
    cachedQueryEnergyByCategory({ ...pace.previous, drill }),
  ]);
  const data = buildEnergyRows(current, period, prevPeriod, toMs - fromMs);

  // Trailing window changes as time passes; historical is immutable.
  const isTrailing = Date.now() - toMs < 2 * 60_000;
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
