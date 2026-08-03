"use client";

import { closestPreset, type RangePreset } from "@/lib/interval";

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
  onPreset,
  onStep,
}: {
  range: RangePreset | null;
  fromMs: number;
  toMs: number;
  onPreset: (preset: RangePreset) => void;
  /** Shift the window back (-1) / forward (+1) by one window-width. */
  onStep: (dir: -1 | 1) => void;
}) {
  // When panned/zoomed (no exact preset), soft-highlight the nearest preset so
  // you see roughly where you are and can snap to it.
  const nearest = range === null ? closestPreset(toMs - fromMs) : null;

  return (
    <div className="flex flex-wrap items-center gap-1.5 text-sm">
      <span className="w-16 shrink-0 text-xs uppercase tracking-wide text-zinc-500">
        Range
      </span>
      <StepButton dir={-1} onClick={() => onStep(-1)} />
      {RANGE_OPTIONS.map((r) => (
        <Chip
          key={r.key}
          active={range === r.key}
          nearest={nearest === r.key}
          onClick={() => onPreset(r.key)}
        >
          {r.label}
        </Chip>
      ))}
      <StepButton dir={1} onClick={() => onStep(1)} />
    </div>
  );
}

// Same pill shape as the presets, flanking the row: step the current window
// one width back / forward. Forward is clamped at `now` upstream.
function StepButton({ dir, onClick }: { dir: -1 | 1; onClick: () => void }) {
  return (
    <button
      type="button"
      onClick={onClick}
      aria-label={dir === -1 ? "Step back one window" : "Step forward one window"}
      className="rounded-full border border-zinc-300 px-2.5 py-1 text-xs leading-4 text-zinc-700 transition-colors hover:border-zinc-500 dark:border-zinc-700 dark:text-zinc-300"
    >
      {dir === -1 ? "‹" : "›"}
    </button>
  );
}

function Chip({
  active,
  nearest,
  onClick,
  children,
}: {
  active: boolean;
  nearest?: boolean;
  onClick: () => void;
  children: React.ReactNode;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      aria-pressed={active}
      className={[
        "rounded-full border px-3 py-1 text-xs transition-colors",
        active
          ? "border-zinc-900 bg-zinc-900 text-white dark:border-zinc-100 dark:bg-zinc-100 dark:text-zinc-900"
          : nearest
            ? "border-zinc-400 bg-zinc-200 text-zinc-700 dark:border-zinc-600 dark:bg-zinc-800 dark:text-zinc-200"
            : "border-zinc-300 text-zinc-700 hover:border-zinc-500 dark:border-zinc-700 dark:text-zinc-300",
      ].join(" ")}
    >
      {children}
    </button>
  );
}
