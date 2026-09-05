import { NextResponse } from "next/server";
import { queryLastPointTime } from "@/lib/influx";
import { HEALTH_CHECKS, evaluateCheck, type HealthCheck } from "@/lib/health";

export const dynamic = "force-dynamic";

export async function GET() {
  const now = new Date();
  let checks: HealthCheck[];
  try {
    const lastTimes = await Promise.all(
      HEALTH_CHECKS.map((c) => queryLastPointTime(c.measurement, c.field, c.lookback)),
    );
    checks = HEALTH_CHECKS.map((c, i) =>
      evaluateCheck(c.name, lastTimes[i], now, c.maxAgeSeconds),
    );
  } catch (err) {
    const note = `influx query failed: ${err instanceof Error ? err.message : String(err)}`;
    checks = HEALTH_CHECKS.map((c) => ({
      name: c.name,
      ok: false,
      ageSeconds: null,
      maxAgeSeconds: c.maxAgeSeconds,
      note,
    }));
  }
  const ok = checks.every((c) => c.ok);
  return NextResponse.json(
    { ok, checks },
    { status: ok ? 200 : 503, headers: { "Cache-Control": "no-store" } },
  );
}
