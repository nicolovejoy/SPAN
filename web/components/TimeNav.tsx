"use client";

import { useRouter, useSearchParams } from "next/navigation";
import { useTransition } from "react";
import { autoInterval } from "@/lib/interval";

const RANGE_OPTIONS = [
  { key: "1h", label: "1h" },
  { key: "6h", label: "6h" },
  { key: "24h", label: "24h" },
  { key: "7d", label: "7d" },
  { key: "30d", label: "30d" },
  { key: "90d", label: "90d" },
  { key: "1y", label: "1y" },
] as const;

export function TimeNav({
  range,
  fromMs,
  toMs,
}: {
  range: string | null;
  fromMs: number;
  toMs: number;
}) {
  const router = useRouter();
  const params = useSearchParams();
  const [pending, startTransition] = useTransition();

  function setRange(rangeKey: string) {
    const next = new URLSearchParams(params.toString());
    next.set("range", rangeKey);
    next.delete("from");
    next.delete("to");
    next.delete("interval");
    startTransition(() => {
      router.replace(`/?${next.toString()}`);
    });
  }

  // Pan by a fraction of the current visible span.
  function pan(fraction: number) {
    const span = Math.max(60_000, toMs - fromMs);
    const shift = Math.round(span * fraction);
    let newFrom = fromMs + shift;
    let newTo = toMs + shift;
    const now = Date.now();
    if (newTo > now) {
      const overshoot = newTo - now;
      newTo -= overshoot;
      newFrom -= overshoot;
    }
    const next = new URLSearchParams(params.toString());
    next.delete("range");
    next.set("from", String(newFrom));
    next.set("to", String(newTo));
    next.set("interval", autoInterval(newFrom, newTo));
    startTransition(() => {
      router.replace(`/?${next.toString()}`);
    });
  }

  const atNow = Math.abs(toMs - Date.now()) < 60_000;

  return (
    <div className="flex flex-col gap-2">
      <div className="flex w-full overflow-hidden rounded-lg border border-zinc-300 bg-zinc-100 text-sm dark:border-zinc-700 dark:bg-zinc-900">
        {RANGE_OPTIONS.map((r, i) => {
          const active = range === r.key;
          return (
            <button
              key={r.key}
              type="button"
              onClick={() => setRange(r.key)}
              className={[
                "flex-1 px-2 py-2.5 text-sm font-medium transition-colors",
                i > 0 && "border-l border-zinc-300 dark:border-zinc-700",
                active
                  ? "bg-zinc-900 text-white dark:bg-zinc-100 dark:text-zinc-900"
                  : "text-zinc-600 hover:bg-zinc-200 dark:text-zinc-300 dark:hover:bg-zinc-800",
              ]
                .filter(Boolean)
                .join(" ")}
            >
              {r.label}
            </button>
          );
        })}
      </div>

      <div className="flex items-stretch gap-2">
        <button
          type="button"
          onClick={() => pan(-0.5)}
          className="flex-1 rounded-lg border border-zinc-300 bg-white py-2.5 text-sm font-medium text-zinc-700 hover:bg-zinc-50 active:bg-zinc-100 dark:border-zinc-700 dark:bg-zinc-950 dark:text-zinc-200 dark:hover:bg-zinc-900"
          aria-label="Pan backward half a window"
        >
          ← back
        </button>
        <button
          type="button"
          onClick={() => pan(0.5)}
          disabled={atNow}
          className="flex-1 rounded-lg border border-zinc-300 bg-white py-2.5 text-sm font-medium text-zinc-700 hover:bg-zinc-50 active:bg-zinc-100 disabled:opacity-40 dark:border-zinc-700 dark:bg-zinc-950 dark:text-zinc-200 dark:hover:bg-zinc-900"
          aria-label="Pan forward half a window"
        >
          fwd →
        </button>
      </div>
      {pending && <div className="text-xs text-zinc-400">updating…</div>}
    </div>
  );
}
