"use client";

import { costForKwh } from "@/lib/rates";
import { EVENT_COLOR, MODE_COLOR } from "@/lib/eventLanes";
import {
  LIST_CAP,
  MODE_LABEL,
  buildListRows,
  fmtPacificDayRange,
  fmtPacificTime,
  formatDurationMs,
  zoomWindow,
  type EventsPayload,
  type ListRow,
} from "@/lib/eventRuns";

const TZ = "America/Los_Angeles";
const dayKey = new Intl.DateTimeFormat("en-CA", { timeZone: TZ });
const monthDay = new Intl.DateTimeFormat("en-US", { timeZone: TZ, month: "short", day: "numeric" });

/** "12:00 PM", or "Sep 5 12:20 AM" when the row crosses a Pacific midnight. */
function cell(ms: number, row: ListRow): string {
  const crosses = dayKey.format(row.fromMs) !== dayKey.format(row.toMs);
  return crosses ? `${monthDay.format(ms)} ${fmtPacificTime(ms)}` : fmtPacificTime(ms);
}

function Swatch({ kind }: { kind: ListRow["kind"] }) {
  const style =
    kind === "bath"
      ? { border: `1.5px solid ${EVENT_COLOR.bath}` }
      : kind === "charge"
        ? { background: EVENT_COLOR.charge, opacity: 0.6 }
        : kind === "ambiguous"
          ? { background: `repeating-linear-gradient(45deg, ${MODE_COLOR.ambiguous} 0 2px, transparent 2px 4px)` }
          : { background: MODE_COLOR[kind] };
  return <i className="inline-block h-2 w-2 rounded-sm" style={style} aria-hidden />;
}

export function EventList({
  data,
  error,
  visible,
  onZoom,
}: {
  data: EventsPayload | null;
  error: boolean;
  visible: { fromMs: number; toMs: number };
  onZoom: (fromMs: number, toMs: number) => void;
}) {
  // The payload covers a padded window (see ExplorerClient), so scope it to the
  // visible one before building rows — filtering ahead of the cap also keeps
  // "showing 50 of N" honest for what the chart is actually showing.
  const overlaps = (r: { fromMs: number; toMs: number }) =>
    r.toMs > visible.fromMs && r.fromMs < visible.toMs;
  const scoped = data
    ? { ...data, modes: data.modes.filter(overlaps), events: data.events.filter(overlaps) }
    : null;
  const { rows, total } = scoped ? buildListRows(scoped, costForKwh) : { rows: [], total: 0 };
  return (
    <section className="flex flex-col gap-1">
      <div className="flex items-baseline justify-between">
        <h2 className="text-xs uppercase tracking-wide text-zinc-500">
          Events · {fmtPacificDayRange(visible.fromMs, visible.toMs)}
        </h2>
        <span className="text-xs text-zinc-400">
          {total > LIST_CAP ? `showing ${LIST_CAP} of ${total} by kWh` : "Rows follow the visible window. Click a row to zoom the chart to it."}
        </span>
      </div>
      {error ? (
        <p className="text-xs text-zinc-400">events unavailable</p>
      ) : rows.length === 0 ? (
        <p className="text-xs text-zinc-400">no events in this window</p>
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full text-sm tabular-nums">
            <thead>
              <tr className="text-left text-[11px] uppercase tracking-wide text-zinc-500">
                <th className="px-2 py-1.5 font-medium">Kind</th>
                <th className="px-2 py-1.5 font-medium">Start</th>
                <th className="px-2 py-1.5 font-medium">End</th>
                <th className="px-2 py-1.5 text-right font-medium">Duration</th>
                <th className="px-2 py-1.5 text-right font-medium">kWh</th>
                <th className="px-2 py-1.5 text-right font-medium">$</th>
                <th className="px-2 py-1.5 font-medium">Detail</th>
                <th className="px-2 py-1.5" />
              </tr>
            </thead>
            <tbody>
              {rows.map((r) => {
                const zoom = () => {
                  const z = zoomWindow(r.fromMs, r.toMs);
                  onZoom(z.fromMs, z.toMs);
                };
                return (
                  <tr
                    key={r.id}
                    role="button"
                    tabIndex={0}
                    className="cursor-pointer border-t border-zinc-100 outline-none hover:bg-zinc-50 focus-visible:bg-zinc-50 dark:border-zinc-900 dark:hover:bg-zinc-900/60 dark:focus-visible:bg-zinc-900/60"
                    onClick={zoom}
                    onKeyDown={(e) => {
                      if (e.key !== "Enter" && e.key !== " ") return;
                      // Space would scroll the page otherwise.
                      e.preventDefault();
                      zoom();
                    }}
                  >
                    <td className="px-2 py-1.5"><span className="inline-flex items-center gap-1.5"><Swatch kind={r.kind} />{MODE_LABEL[r.kind]}</span></td>
                    <td className="px-2 py-1.5">{cell(r.fromMs, r)}</td>
                    <td className="px-2 py-1.5">{cell(r.toMs, r)}</td>
                    <td className="px-2 py-1.5 text-right">{formatDurationMs(r.toMs - r.fromMs)}</td>
                    <td className="px-2 py-1.5 text-right">{r.kwh.toFixed(1)}</td>
                    <td className="px-2 py-1.5 text-right">{r.costDollars.toFixed(2)}</td>
                    <td className="px-2 py-1.5 text-xs text-zinc-500">{r.detail}</td>
                    <td className="px-2 py-1.5 text-right text-xs text-zinc-400">zoom →</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}
