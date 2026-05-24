"use client";

import { autoInterval } from "@/lib/interval";
import { SegmentedControl } from "@/components/SegmentedControl";
import { useUpdateParams } from "@/components/hooks/useUpdateParams";

const RANGE_OPTIONS = [
  { key: "1h", label: "1h" },
  { key: "6h", label: "6h" },
  { key: "24h", label: "24h" },
  { key: "7d", label: "7d" },
  { key: "30d", label: "30d" },
  { key: "90d", label: "90d" },
  { key: "1y", label: "1y" },
] as const;

type RangeKey = (typeof RANGE_OPTIONS)[number]["key"];

/** Slide the window by `fraction` of its current span, clamped so it never
 * extends past `now`. Returns the new ms-epoch window. */
function panBy(
  fromMs: number,
  toMs: number,
  fraction: number,
  now: number,
): { fromMs: number; toMs: number } {
  const span = Math.max(60_000, toMs - fromMs);
  const shift = Math.round(span * fraction);
  let newFrom = fromMs + shift;
  let newTo = toMs + shift;
  if (newTo > now) {
    const overshoot = newTo - now;
    newTo -= overshoot;
    newFrom -= overshoot;
  }
  return { fromMs: newFrom, toMs: newTo };
}

export function TimeNav({
  range,
  fromMs,
  toMs,
}: {
  range: string | null;
  fromMs: number;
  toMs: number;
}) {
  const { update, pending } = useUpdateParams();

  const setRange = (rangeKey: RangeKey) =>
    update({ range: rangeKey, from: null, to: null, interval: null });

  const pan = (fraction: number) => {
    const w = panBy(fromMs, toMs, fraction, Date.now());
    update({
      range: null,
      from: String(w.fromMs),
      to: String(w.toMs),
      interval: autoInterval(w.fromMs, w.toMs),
    });
  };

  const atNow = Math.abs(toMs - Date.now()) < 60_000;
  const buttonCls =
    "flex-1 rounded-lg border border-zinc-300 bg-white py-2.5 text-sm font-medium text-zinc-700 hover:bg-zinc-50 active:bg-zinc-100 disabled:opacity-40 dark:border-zinc-700 dark:bg-zinc-950 dark:text-zinc-200 dark:hover:bg-zinc-900";

  return (
    <div className="flex flex-col gap-2">
      <SegmentedControl
        options={RANGE_OPTIONS}
        active={(range as RangeKey | null) ?? null}
        onSelect={setRange}
        size="md"
      />
      <div className="flex items-stretch gap-2">
        <button
          type="button"
          onClick={() => pan(-0.5)}
          className={buttonCls}
          aria-label="Pan backward half a window"
        >
          ← back
        </button>
        <button
          type="button"
          onClick={() => pan(0.5)}
          disabled={atNow}
          className={buttonCls}
          aria-label="Pan forward half a window"
        >
          fwd →
        </button>
      </div>
      {pending && <div className="text-xs text-zinc-400">updating…</div>}
    </div>
  );
}
