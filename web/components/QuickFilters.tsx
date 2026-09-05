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
  drill,
  onDrill,
  events,
  onEvents,
}: {
  show: string[];
  onChange: (next: string[]) => void;
  /** Category currently expanded into its circuits, or null (#12). */
  drill: string | null;
  onDrill: (next: string | null) => void;
  events: boolean;
  onEvents: (on: boolean) => void;
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
      {CHIPS.map((cat) => {
        // The drill affordance only shows on chips whose line is actually on
        // the chart — drilling into a hidden category would draw nothing.
        const visible = allActive || selected.has(cat);
        const drilled = drill === cat;
        return (
          <span key={cat} className="inline-flex items-center">
            <Chip
              active={!allActive && selected.has(cat)}
              dim={!allActive && !selected.has(cat)}
              onClick={() => navigate(nextShow(show, cat))}
            >
              {cat}
            </Chip>
            {visible && (
              <DrillToggle
                drilled={drilled}
                label={cat}
                // Only one category drills at a time: picking a new one
                // replaces the old rather than adding to it.
                onClick={() => onDrill(drilled ? null : cat)}
              />
            )}
          </span>
        );
      })}
      <span className="mx-1 text-zinc-300 dark:text-zinc-700" aria-hidden>|</span>
      <Chip
        active={events}
        onClick={() => onEvents(!events)}
        title="Heat-pump mode strip + bath/EV events under the chart"
      >
        Events
      </Chip>
    </div>
  );
}

/** Caret appended to a category chip; becomes an ✕ once drilled. */
function DrillToggle({
  drilled,
  label,
  onClick,
}: {
  drilled: boolean;
  label: string;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      aria-pressed={drilled}
      title={drilled ? `Hide ${label} circuits` : `Show ${label} circuits`}
      aria-label={drilled ? `Hide ${label} circuits` : `Show ${label} circuits`}
      className={[
        "-ml-1 rounded-full border px-1.5 py-1 text-[10px] leading-none transition-colors",
        drilled
          ? "border-zinc-900 bg-zinc-900 text-white dark:border-zinc-100 dark:bg-zinc-100 dark:text-zinc-900"
          : "border-zinc-300 text-zinc-500 hover:border-zinc-500 dark:border-zinc-700 dark:text-zinc-400",
      ].join(" ")}
    >
      {drilled ? "✕" : "⌄"}
    </button>
  );
}

function Chip({
  active,
  dim,
  onClick,
  children,
  title,
}: {
  active?: boolean;
  dim?: boolean;
  onClick: () => void;
  children: React.ReactNode;
  title?: string;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      aria-pressed={active}
      title={title}
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
