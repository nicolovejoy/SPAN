"use client";

import { useRouter, useSearchParams } from "next/navigation";
import { useTransition } from "react";
import { CATEGORIES } from "@/lib/categories";
import { INTERVAL_ORDER, type IntervalKey } from "@/lib/interval";
import type { GroupBy } from "@/lib/influx";

const RANGE_OPTIONS = [
  { key: "1h", label: "1h" },
  { key: "6h", label: "6h" },
  { key: "24h", label: "24h" },
  { key: "7d", label: "7d" },
  { key: "30d", label: "30d" },
  { key: "90d", label: "90d" },
  { key: "1y", label: "1y" },
] as const;

const GROUP_OPTIONS: { key: GroupBy; label: string }[] = [
  { key: "all", label: "All" },
  { key: "category", label: "Category" },
  { key: "circuit", label: "Circuit" },
];

export function Filters({
  range,
  interval,
  intervalAuto,
  groupBy,
  categories,
}: {
  range: string | null;
  interval: IntervalKey;
  intervalAuto: boolean;
  groupBy: GroupBy;
  categories: string[];
}) {
  const router = useRouter();
  const params = useSearchParams();
  const [pending, startTransition] = useTransition();

  function update(patch: Record<string, string | null>) {
    const next = new URLSearchParams(params.toString());
    for (const [k, v] of Object.entries(patch)) {
      if (v === null) next.delete(k);
      else next.set(k, v);
    }
    next.delete("from");
    next.delete("to");
    startTransition(() => {
      router.replace(`/?${next.toString()}`);
    });
  }

  function toggleCategory(cat: string) {
    const set = new Set(categories);
    if (set.has(cat)) set.delete(cat);
    else set.add(cat);
    update({
      categories: set.size ? Array.from(set).join(",") : null,
    });
  }

  return (
    <div className="flex flex-col gap-3 text-sm">
      <FilterRow label="Range">
        {RANGE_OPTIONS.map((r) => (
          <Pill
            key={r.key}
            active={range === r.key}
            onClick={() => update({ range: r.key })}
          >
            {r.label}
          </Pill>
        ))}
      </FilterRow>

      <FilterRow label="Bucket">
        <Pill
          active={intervalAuto}
          onClick={() => update({ interval: null })}
        >
          auto ({interval})
        </Pill>
        {INTERVAL_ORDER.map((i) => (
          <Pill
            key={i}
            active={!intervalAuto && interval === i}
            onClick={() => update({ interval: i })}
          >
            {i}
          </Pill>
        ))}
      </FilterRow>

      <FilterRow label="Group by">
        {GROUP_OPTIONS.map((g) => (
          <Pill
            key={g.key}
            active={groupBy === g.key}
            onClick={() => update({ groupBy: g.key === "category" ? null : g.key })}
          >
            {g.label}
          </Pill>
        ))}
      </FilterRow>

      <FilterRow label="Categories">
        {CATEGORIES.map((c) => (
          <Pill
            key={c}
            active={categories.length === 0 || categories.includes(c)}
            dim={categories.length > 0 && !categories.includes(c)}
            onClick={() => toggleCategory(c)}
          >
            {c}
          </Pill>
        ))}
      </FilterRow>

      {pending && <div className="text-xs text-zinc-400">updating…</div>}
    </div>
  );
}

function FilterRow({
  label,
  children,
}: {
  label: string;
  children: React.ReactNode;
}) {
  return (
    <div className="flex flex-wrap items-center gap-1.5">
      <span className="w-20 shrink-0 text-xs uppercase tracking-wide text-zinc-500">
        {label}
      </span>
      {children}
    </div>
  );
}

function Pill({
  active,
  dim,
  onClick,
  children,
}: {
  active?: boolean;
  dim?: boolean;
  onClick: () => void;
  children: React.ReactNode;
}) {
  return (
    <button
      onClick={onClick}
      className={[
        "rounded-full border px-3 py-1 text-xs transition-colors",
        active
          ? "border-zinc-900 bg-zinc-900 text-white dark:border-zinc-100 dark:bg-zinc-100 dark:text-zinc-900"
          : dim
            ? "border-zinc-200 text-zinc-400 dark:border-zinc-800 dark:text-zinc-600"
            : "border-zinc-300 text-zinc-700 hover:border-zinc-500 dark:border-zinc-700 dark:text-zinc-300",
      ].join(" ")}
    >
      {children}
    </button>
  );
}
