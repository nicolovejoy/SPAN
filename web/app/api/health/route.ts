import { NextResponse } from "next/server";
import { queryLastPointTime } from "@/lib/influx";
import {
  BACKUP_MAX_AGE_S,
  COLLECTOR_MAX_AGE_S,
  evaluateCheck,
  type HealthCheck,
} from "@/lib/health";

export const dynamic = "force-dynamic";

export async function GET() {
  const now = new Date();
  let checks: HealthCheck[];
  try {
    const [collector, backup] = await Promise.all([
      queryLastPointTime("circuit", "power_w", "1h"),
      queryLastPointTime("backup_snapshot", "ok", "14d"),
    ]);
    checks = [
      evaluateCheck("collector", collector, now, COLLECTOR_MAX_AGE_S),
      evaluateCheck("backup", backup, now, BACKUP_MAX_AGE_S),
    ];
  } catch (err) {
    const note = `influx query failed: ${err instanceof Error ? err.message : String(err)}`;
    checks = [
      { name: "collector", ok: false, ageSeconds: null, maxAgeSeconds: COLLECTOR_MAX_AGE_S, note },
      { name: "backup", ok: false, ageSeconds: null, maxAgeSeconds: BACKUP_MAX_AGE_S, note },
    ];
  }
  const ok = checks.every((c) => c.ok);
  return NextResponse.json(
    { ok, checks },
    { status: ok ? 200 : 503, headers: { "Cache-Control": "no-store" } },
  );
}
