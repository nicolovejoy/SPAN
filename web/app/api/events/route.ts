import { NextResponse } from "next/server";
import { cachedQueryEvents } from "@/lib/queryCache";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

/** Mode runs + bath/charge events for a window. Response is the EventsPayload
 *  at the top level: { modes, events, modesTruncated }. */
export async function GET(request: Request) {
  const url = new URL(request.url);
  const fromMs = Number(url.searchParams.get("from"));
  const toMs = Number(url.searchParams.get("to"));
  if (!Number.isFinite(fromMs) || !Number.isFinite(toMs) || fromMs >= toMs) {
    return NextResponse.json(
      { error: "invalid from/to (ms epoch, from < to required)" },
      { status: 400 },
    );
  }
  const data = await cachedQueryEvents({ fromMs, toMs });
  const isTrailing = Date.now() - toMs < 2 * 60_000;
  const maxAge = isTrailing ? 60 : 86400;
  return NextResponse.json(data, {
    headers: { "Cache-Control": `public, max-age=${maxAge}, stale-while-revalidate=3600` },
  });
}
