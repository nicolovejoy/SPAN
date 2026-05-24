"use client";

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

export function TimeNav({ range }: { range: string | null }) {
  const { update, pending } = useUpdateParams();

  const setRange = (rangeKey: RangeKey) =>
    update({ range: rangeKey, from: null, to: null, interval: null });

  return (
    <div className="flex flex-wrap items-center gap-1.5 text-sm">
      <span className="w-16 shrink-0 text-xs uppercase tracking-wide text-zinc-500">
        Range
      </span>
      {RANGE_OPTIONS.map((r) => (
        <Chip
          key={r.key}
          active={range === r.key}
          onClick={() => setRange(r.key)}
        >
          {r.label}
        </Chip>
      ))}
      {pending && <span className="text-xs text-zinc-400">updating…</span>}
    </div>
  );
}

function Chip({
  active,
  onClick,
  children,
}: {
  active: boolean;
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
          : "border-zinc-300 text-zinc-700 hover:border-zinc-500 dark:border-zinc-700 dark:text-zinc-300",
      ].join(" ")}
    >
      {children}
    </button>
  );
}
