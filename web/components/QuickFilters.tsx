"use client";

import { useRouter, useSearchParams } from "next/navigation";
import { useTransition } from "react";

const CHIPS = ["Lights", "HVAC", "Car", "Appliances", "Else"] as const;

export function QuickFilters({ show }: { show: string[] }) {
  const router = useRouter();
  const params = useSearchParams();
  const [pending, startTransition] = useTransition();

  const selected = new Set(show);
  const allActive = show.length === 0;

  function navigate(nextShow: string[] | null) {
    const next = new URLSearchParams(params.toString());
    if (nextShow === null || nextShow.length === 0) next.delete("show");
    else next.set("show", nextShow.join(","));
    startTransition(() => {
      router.replace(`/?${next.toString()}`);
    });
  }

  function toggle(cat: string) {
    if (allActive) {
      // First click after "All": isolate just this one.
      navigate([cat]);
      return;
    }
    const set = new Set(show);
    if (set.has(cat)) set.delete(cat);
    else set.add(cat);
    navigate(Array.from(set));
  }

  return (
    <div className="flex flex-wrap items-center gap-1.5 text-sm">
      <span className="w-20 shrink-0 text-xs uppercase tracking-wide text-zinc-500">
        Show
      </span>

      <Chip active={allActive} onClick={() => navigate(null)}>
        All
      </Chip>

      {CHIPS.map((cat) => (
        <Chip
          key={cat}
          active={!allActive && selected.has(cat)}
          dim={!allActive && !selected.has(cat)}
          onClick={() => toggle(cat)}
        >
          {cat}
        </Chip>
      ))}

      {pending && <span className="text-xs text-zinc-400">updating…</span>}
    </div>
  );
}

function Chip({
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
