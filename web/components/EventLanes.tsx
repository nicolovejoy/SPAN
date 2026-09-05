"use client";

import { useEffect, useState } from "react";
import {
  EVENT_COLOR,
  LANE_H,
  MODE_COLOR,
  labelFits,
  layoutBlocks,
  type XOf,
} from "@/lib/eventLanes";
import {
  MODE_LABEL,
  bathsWithin,
  fmtPacificRange,
  formatDurationMs,
  type EventItem,
  type EventsPayload,
  type ModeRun,
} from "@/lib/eventRuns";

type Hover =
  | { kind: "mode"; run: ModeRun; x: number }
  | { kind: "event"; ev: EventItem; x: number }
  | null;

const kw = (w: number) => `${(w / 1000).toFixed(1)} kW`;

const itemOf = (h: NonNullable<Hover>): ModeRun | EventItem =>
  h.kind === "mode" ? h.run : h.ev;

export function EventLanes({
  data,
  visible,
  xOf,
  width,
}: {
  data: EventsPayload | null;
  visible: { fromMs: number; toMs: number };
  xOf: XOf;
  /** Plot-area width in px (chart width minus the right price scale). */
  width: number;
}) {
  const [hover, setHover] = useState<Hover>(null);
  // onMouseLeave never fires if the hovered block unmounts under the cursor, so
  // a new payload would otherwise leave a tooltip describing the old one.
  useEffect(() => {
    setHover(null);
  }, [data]);
  // Click toggles: tapping the open block again dismisses the tooltip, which is
  // the only way to close it on touch (no mouseleave).
  const toggle = (next: NonNullable<Hover>) =>
    setHover((cur) =>
      cur && cur.kind === next.kind && itemOf(cur).fromMs === itemOf(next).fromMs ? null : next,
    );
  const modes = data ? layoutBlocks(data.modes, visible, xOf) : [];
  const events = data ? layoutBlocks(data.events, visible, xOf) : [];

  return (
    <div className="relative select-none" style={{ width }}>
      {/* Row 1: heat-pump mode */}
      <div className="relative border-t border-zinc-200 dark:border-zinc-800" style={{ height: LANE_H }}>
        <Gutter>HP mode</Gutter>
        {data?.modesTruncated && (
          <span className="absolute left-16 top-1 text-[10px] text-zinc-400">zoom in for HP modes</span>
        )}
        <svg width={width} height={LANE_H} className="block">
          <defs>
            <pattern id="lane-hatch" width="5" height="5" patternUnits="userSpaceOnUse" patternTransform="rotate(45)">
              <rect width="2" height="5" fill={MODE_COLOR.ambiguous} />
            </pattern>
          </defs>
          {modes.map((b) => (
            <g
              key={`${b.item.mode}:${b.item.fromMs}`}
              onMouseEnter={() => setHover({ kind: "mode", run: b.item, x: b.x + b.w / 2 })}
              onMouseLeave={() => setHover(null)}
              onClick={() => toggle({ kind: "mode", run: b.item, x: b.x + b.w / 2 })}
            >
              <HitRect x={b.x} w={b.w} />
              <rect
                x={b.x}
                y={4}
                width={b.w}
                height={LANE_H - 8}
                fill={b.item.mode === "ambiguous" ? "url(#lane-hatch)" : MODE_COLOR[b.item.mode]}
              />
            </g>
          ))}
        </svg>
      </div>

      {/* Row 2: bath + EV events */}
      <div className="relative" style={{ height: LANE_H }}>
        <Gutter>Events</Gutter>
        <svg width={width} height={LANE_H} className="block">
          {events.map((b) => {
            const bath = b.item.kind === "bath";
            const color = EVENT_COLOR[b.item.kind];
            return (
              <g
                key={`${b.item.kind}:${b.item.fromMs}`}
                onMouseEnter={() => setHover({ kind: "event", ev: b.item, x: b.x + b.w / 2 })}
                onMouseLeave={() => setHover(null)}
                onClick={() => toggle({ kind: "event", ev: b.item, x: b.x + b.w / 2 })}
              >
                <HitRect x={b.x} w={b.w} />
                <rect
                  x={b.x}
                  y={5}
                  width={b.w}
                  height={LANE_H - 10}
                  rx={2}
                  fill={bath ? "none" : color}
                  fillOpacity={bath ? 1 : 0.25}
                  stroke={color}
                  strokeWidth={bath ? 1.5 : 1}
                />
                {labelFits(b.w) && (
                  <text
                    x={b.x + b.w / 2}
                    y={LANE_H / 2 + 3.5}
                    fontSize={9}
                    textAnchor="middle"
                    fill={bath ? color : "currentColor"}
                    className="pointer-events-none"
                  >
                    {bath ? "bath" : `EV ${b.item.kwh.toFixed(1)} kWh`}
                  </text>
                )}
              </g>
            );
          })}
        </svg>
      </div>

      {hover && data && (
        <div
          className="pointer-events-none absolute z-10 rounded border border-zinc-300 bg-white/95 px-2 py-1 text-[11px] shadow dark:border-zinc-700 dark:bg-zinc-900/95"
          style={{ left: Math.max(0, Math.min(width - 220, hover.x - 110)), top: -8, transform: "translateY(-100%)", width: 220 }}
        >
          {hover.kind === "mode" ? <ModeTip run={hover.run} events={data.events} /> : <EventTip ev={hover.ev} />}
        </div>
      )}
    </div>
  );
}

/** Invisible full-lane-height target behind a block. A bath block is
 *  `fill="none"`, so under SVG's default `visiblePainted` its group would only
 *  be hoverable on the 1.5px stroke; a 1px-wide block is unhittable either way.
 *  Drawn first so it sits behind, and widened to a minimum 8px. */
function HitRect({ x, w }: { x: number; w: number }) {
  return <rect x={x} y={0} width={Math.max(w, 8)} height={LANE_H} fill="transparent" />;
}

function Gutter({ children }: { children: React.ReactNode }) {
  return (
    <span className="pointer-events-none absolute left-1 top-1 z-[1] text-[10px] uppercase tracking-wide text-zinc-400 dark:text-zinc-500">
      {children}
    </span>
  );
}

function ModeTip({ run, events }: { run: ModeRun; events: EventItem[] }) {
  // Count only — a long hot-water run can hold several baths, and listing every
  // range overflows the fixed 220px tooltip.
  const nBaths = run.mode === "hot_water" ? bathsWithin(run, events).length : 0;
  return (
    <>
      <div><b>{MODE_LABEL[run.mode]}</b> · {fmtPacificRange(run.fromMs, run.toMs)}</div>
      <div className="text-zinc-500">
        {formatDurationMs(run.toMs - run.fromMs)} · {run.kwh.toFixed(1)} kWh · HP mean {kw(run.hpMeanW)} · max {kw(run.hpMaxW)} · aux {run.auxMeanW > 50 ? "on" : "off"}
      </div>
      {nBaths > 0 && (
        <div className="text-zinc-500">
          contains {nBaths} bath{nBaths === 1 ? "" : "s"}
        </div>
      )}
    </>
  );
}

function EventTip({ ev }: { ev: EventItem }) {
  return (
    <>
      <div><b>{MODE_LABEL[ev.kind]}</b> · {fmtPacificRange(ev.fromMs, ev.toMs)}</div>
      <div className="text-zinc-500">
        {formatDurationMs(ev.toMs - ev.fromMs)} · {ev.kwh.toFixed(1)} kWh · ${ev.costDollars.toFixed(2)} · max {kw(ev.maxW)}
        {ev.kind === "bath" ? ` · aux ${ev.auxActive ? "on" : "off"}` : ""}
      </div>
    </>
  );
}
