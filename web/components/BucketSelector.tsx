"use client";

import { INTERVAL_ORDER, type IntervalKey } from "@/lib/interval";
import { useUpdateParams } from "@/components/hooks/useUpdateParams";

const AUTO_KEY = "auto" as const;
type BucketKey = typeof AUTO_KEY | IntervalKey;

export function BucketSelector({
  interval,
  intervalAuto,
}: {
  interval: IntervalKey;
  intervalAuto: boolean;
}) {
  const { update, pending } = useUpdateParams();

  const active: BucketKey = intervalAuto ? AUTO_KEY : interval;

  const onSelect = (key: BucketKey) =>
    update({ interval: key === AUTO_KEY ? null : key });

  return (
    <div className="flex flex-wrap items-center gap-1.5 text-sm">
      <span className="w-16 shrink-0 text-xs uppercase tracking-wide text-zinc-500">
        Bucket
      </span>
      <Chip active={active === AUTO_KEY} onClick={() => onSelect(AUTO_KEY)}>
        auto ({interval})
      </Chip>
      {INTERVAL_ORDER.map((i) => (
        <Chip key={i} active={active === i} onClick={() => onSelect(i)}>
          {i}
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
