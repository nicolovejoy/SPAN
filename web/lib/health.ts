// Health checks for /api/health — the shape prompt-lab's uptime convention
// expects: { ok, checks: [{ name, ok, ... }] }, HTTP 503 when any check fails.
//
// Every check alarms on artifact age, never on a job's own success report:
//   collector  — age of the newest raw `circuit` point (collector polls every 30s)
//   backup     — age of the newest `backup_snapshot` point; its timestamp is the
//                restic snapshot's own time, published by pi/backup/backup.sh
//   weather    — age of the newest `weather` point (weather_poller loops hourly)
//   hvac_mode  — age of the newest `hvac_mode` interval (classifier loops every 600s,
//                writes only completed 5-min intervals)

export type HealthCheck = {
  name: string;
  ok: boolean;
  ageSeconds: number | null;
  maxAgeSeconds: number;
  note?: string;
};

/** One artifact-age check per Pi service that writes on a cadence. Order is
 *  the order /api/health reports them. Irregular writers (bath_event,
 *  charge_event) get no check — silence is normal for them. */
export type CheckSpec = {
  name: string;
  measurement: string;
  field: string;
  /** Influx range start for the "newest point" query, e.g. "1h", "2d". */
  lookback: string;
  maxAgeSeconds: number;
};

export const HEALTH_CHECKS: CheckSpec[] = [
  // 10× the 30s poll: a dead collector alarms within minutes, one slow poll doesn't flap.
  { name: "collector", measurement: "circuit", field: "power_w", lookback: "1h", maxAgeSeconds: 300 },
  // Nightly at 03:30 + generous grace for a slow run or a late timer.
  { name: "backup", measurement: "backup_snapshot", field: "ok", lookback: "14d", maxAgeSeconds: 30 * 3600 },
  // weather_poller loops hourly.
  { name: "weather", measurement: "weather", field: "temp_f", lookback: "2d", maxAgeSeconds: 3 * 3600 },
  // hvac_classifier loops every 600s and writes only completed 5-min intervals,
  // so a healthy newest point is 5–15 min old; 45 min is 3× the worst healthy case.
  { name: "hvac_mode", measurement: "hvac_mode", field: "mode", lookback: "2d", maxAgeSeconds: 45 * 60 },
];

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
