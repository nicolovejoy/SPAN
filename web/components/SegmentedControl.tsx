"use client";

export type SegmentOption<T extends string> = {
  key: T;
  label: string;
};

/**
 * iOS-style segmented control. Used for Range (full-width, big tap targets)
 * and Bucket (compact). Single-select, always one active.
 */
export function SegmentedControl<T extends string>({
  options,
  active,
  onSelect,
  size = "md",
  label,
}: {
  options: ReadonlyArray<SegmentOption<T>>;
  active: T | null;
  onSelect: (key: T) => void;
  size?: "sm" | "md";
  label?: string;
}) {
  const padding = size === "sm" ? "px-2 py-1.5 text-xs" : "px-2 py-2.5 text-sm";

  return (
    <div className="flex items-center gap-3">
      {label && (
        <span className="w-16 shrink-0 text-xs uppercase tracking-wide text-zinc-500">
          {label}
        </span>
      )}
      <div className="flex w-full overflow-hidden rounded-lg border border-zinc-300 bg-zinc-100 dark:border-zinc-700 dark:bg-zinc-900">
        {options.map((opt, i) => {
          const isActive = active === opt.key;
          return (
            <button
              key={opt.key}
              type="button"
              onClick={() => onSelect(opt.key)}
              aria-pressed={isActive}
              className={[
                "flex-1 font-medium transition-colors",
                padding,
                i > 0 && "border-l border-zinc-300 dark:border-zinc-700",
                isActive
                  ? "bg-zinc-900 text-white dark:bg-zinc-100 dark:text-zinc-900"
                  : "text-zinc-600 hover:bg-zinc-200 dark:text-zinc-300 dark:hover:bg-zinc-800",
              ]
                .filter(Boolean)
                .join(" ")}
            >
              {opt.label}
            </button>
          );
        })}
      </div>
    </div>
  );
}
