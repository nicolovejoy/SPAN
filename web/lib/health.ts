// Health checks for /api/health — the shape prompt-lab's uptime convention
// expects: { ok, checks: [{ name, ok, ... }] }, HTTP 503 when any check fails.
//
// Both checks alarm on artifact age, never on a job's own success report:
//   collector — age of the newest raw `circuit` point (collector polls every 30s)
//   backup    — age of the newest `backup_snapshot` point; its timestamp is the
//               restic snapshot's own time, published by pi/backup/backup.sh

export type HealthCheck = {
  name: string;
  ok: boolean;
  ageSeconds: number | null;
  maxAgeSeconds: number;
  note?: string;
};

// 10× the 30s poll cadence: a dead collector alarms within minutes, a single
// slow poll doesn't flap.
export const COLLECTOR_MAX_AGE_S = 300;

// Nightly at 03:30 + generous grace for a slow run or a late timer.
export const BACKUP_MAX_AGE_S = 30 * 3600;

export function evaluateCheck(
  name: string,
  lastTime: string | null,
  now: Date,
  maxAgeSeconds: number,
): HealthCheck {
  if (lastTime === null) {
    return {
      name,
      ok: false,
      ageSeconds: null,
      maxAgeSeconds,
      note: "no data point found",
    };
  }
  const t = Date.parse(lastTime);
  if (Number.isNaN(t)) {
    return {
      name,
      ok: false,
      ageSeconds: null,
      maxAgeSeconds,
      note: `unparseable timestamp: ${lastTime}`,
    };
  }
  const ageSeconds = Math.max(0, Math.round((now.getTime() - t) / 1000));
  return { name, ok: ageSeconds <= maxAgeSeconds, ageSeconds, maxAgeSeconds };
}
