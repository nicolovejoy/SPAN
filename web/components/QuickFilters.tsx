"use client";

const CHIPS = ["Lights", "HVAC", "Car", "Appliances", "Else"] as const;

function nextShow(current: string[], cat: string): string[] {
  const allActive = current.length === 0;
  if (allActive) return [cat]; // first click after "All" isolates
  const set = new Set(current);
  if (set.has(cat)) set.delete(cat);
  else set.add(cat);
  return Array.from(set);
}

export function QuickFilters({
  show,
  onChange,
}: {
  show: string[];
  onChange: (next: string[]) => void;
}) {
  const navigate = (next: string[]) => onChange(next);

  const allActive = show.length === 0;
  const selected = new Set(show);

  return (
    <div className="flex flex-wrap items-center gap-1.5 text-sm">
      <span className="w-16 shrink-0 text-xs uppercase tracking-wide text-zinc-500">
        Show
      </span>
      <Chip active={allActive} onClick={() => navigate([])}>
        All
      </Chip>
      {CHIPS.map((cat) => (
        <Chip
          key={cat}
          active={!allActive && selected.has(cat)}
          dim={!allActive && !selected.has(cat)}
          onClick={() => navigate(nextShow(show, cat))}
        >
          {cat}
        </Chip>
      ))}
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
      type="button"
      onClick={onClick}
      aria-pressed={active}
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
