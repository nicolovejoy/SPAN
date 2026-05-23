import { NextResponse } from "next/server";
import { queryPower, type GroupBy } from "@/lib/influx";
import { INTERVAL_ORDER, type IntervalKey } from "@/lib/interval";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

const isInterval = (v: string): v is IntervalKey =>
  (INTERVAL_ORDER as string[]).includes(v);
const isGroupBy = (v: string): v is GroupBy =>
  ["all", "category", "circuit"].includes(v);

export async function GET(request: Request) {
  const url = new URL(request.url);
  const fromMs = Number(url.searchParams.get("from"));
  const toMs = Number(url.searchParams.get("to"));
  const intervalRaw = url.searchParams.get("interval") ?? "1h";
  const groupByRaw = url.searchParams.get("groupBy") ?? "category";
  const categoriesRaw = url.searchParams.get("categories");

  if (!Number.isFinite(fromMs) || !Number.isFinite(toMs) || fromMs >= toMs) {
    return NextResponse.json(
      { error: "invalid from/to (ms epoch, from < to required)" },
      { status: 400 }
    );
  }
  if (!isInterval(intervalRaw)) {
    return NextResponse.json({ error: `invalid interval ${intervalRaw}` }, { status: 400 });
  }
  if (!isGroupBy(groupByRaw)) {
    return NextResponse.json({ error: `invalid groupBy ${groupByRaw}` }, { status: 400 });
  }

  const data = await queryPower({
    fromMs,
    toMs,
    interval: intervalRaw,
    groupBy: groupByRaw,
    categories: categoriesRaw ? categoriesRaw.split(",").filter(Boolean) : undefined,
  });

  return NextResponse.json({ data });
}
