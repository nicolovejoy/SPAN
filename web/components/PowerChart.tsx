"use client";

import { useEffect, useRef, useState } from "react";
import {
  createChart,
  LineSeries,
  LineStyle,
  TickMarkType,
  type IChartApi,
  type ISeriesApi,
  type TimeRangeChangeEventHandler,
  type Time,
  type UTCTimestamp,
  type WhitespaceData,
} from "lightweight-charts";
import { intervalSeconds } from "@/lib/interval";
import { fetchSeriesCached } from "@/lib/clientFetch";
import type { SeriesPoint } from "@/lib/influx";
import type { DashState } from "@/lib/url-state";

/** Called when a pan/zoom settles, with the visible sub-window (real-UTC ms).
 *  Drives the table + header only — it never changes the chart's loaded
 *  window, so there is no gesture→fetch→setVisibleRange feedback loop. */
export type VisibleChange = (fromMs: number, toMs: number) => void;

const CATEGORY_COLORS: Record<string, string> = {
  HVAC: "#ef4444",
  Car: "#3b82f6",
  Lights: "#eab308",
  Appliances: "#f59e0b",
  Else: "#6b7280",
};
const CATEGORY_ORDER = ["HVAC", "Car", "Lights", "Appliances", "Else"] as const;
const SUM_COLOR = "#525252";    // neutral; reads as derived in light + dark
const TOTAL_COLOR = "#9ca3af";  // dotted reference, intentionally low-contrast

type Point = { time: UTCTimestamp; value: number };
type SeriesEntry = Point | WhitespaceData<UTCTimestamp>;

function toUtc(iso: string): UTCTimestamp {
  return Math.floor(new Date(iso).getTime() / 1000) as UTCTimestamp;
}

// lightweight-charts has no timezone support — it renders every timestamp as
// UTC. To put Pacific wall-clock on the axis we shift each real-UTC second by
// that instant's Pacific offset before handing it to the chart, so the
// library's "UTC" clock reads as Seattle local. All navigation / fetch / URL
// math stays in true UTC; only values crossing the chart boundary are
// transformed (toChartData / setVisibleRange out, fromDisplay back in).
const DISPLAY_TZ = "America/Los_Angeles";

// Seconds to add to a real-UTC instant so it reads as Pacific wall-clock.
// Computed per-instant via Intl so DST flips across a wide range stay correct
// (e.g. -8h in winter, -7h in summer). Negative west of UTC.
function tzOffsetSec(utcMs: number): number {
  const dtf = new Intl.DateTimeFormat("en-US", {
    timeZone: DISPLAY_TZ,
    hourCycle: "h23",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
  const p: Record<string, string> = {};
  for (const part of dtf.formatToParts(new Date(utcMs))) p[part.type] = part.value;
  const asUtc = Date.UTC(+p.year, +p.month - 1, +p.day, +p.hour, +p.minute, +p.second);
  return Math.round((asUtc - utcMs) / 1000);
}

// real UTC second → chart "display" second (Pacific wall-clock as fake-UTC).
function toDisplay(realSec: number): UTCTimestamp {
  return (realSec + tzOffsetSec(realSec * 1000)) as UTCTimestamp;
}

// chart "display" second → real UTC second. One refine pass nails the DST edge.
function fromDisplay(dispSec: number): number {
  let real = dispSec - tzOffsetSec(dispSec * 1000);
  real = dispSec - tzOffsetSec(real * 1000);
  return real;
}

// Shift a built series array onto the chart's Pacific-as-UTC clock right before
// setData. Whitespace sentinels ({time} only) carry through without a value.
function toChartData(entries: SeriesEntry[]): SeriesEntry[] {
  return entries.map((e) =>
    "value" in e
      ? { time: toDisplay(Number(e.time)), value: e.value }
      : { time: toDisplay(Number(e.time)) },
  );
}

// Axis formatting. Chart times are already Pacific-as-fake-UTC (see toDisplay),
// so read them with getUTC* — the wall-clock is baked into the number.
const WD = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
const MO = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
const hhmm = (d: Date) => `${d.getUTCHours()}:${String(d.getUTCMinutes()).padStart(2, "0")}`;

// Day boundaries (and month/year starts) render as a dated label so a window
// spanning midnight gets a clear demarcation under the hours; intra-day ticks
// stay HH:mm.
function tickFmt(time: Time, type: TickMarkType): string {
  const d = new Date(Number(time) * 1000);
  switch (type) {
    case TickMarkType.Year:
      return String(d.getUTCFullYear());
    case TickMarkType.Month:
      return `${MO[d.getUTCMonth()]} ${d.getUTCFullYear()}`;
    case TickMarkType.DayOfMonth:
      return `${WD[d.getUTCDay()]} ${MO[d.getUTCMonth()]} ${d.getUTCDate()}`;
    default:
      return hhmm(d);
  }
}

// Crosshair tooltip — full date + time, Pacific.
function crosshairFmt(time: Time): string {
  const d = new Date(Number(time) * 1000);
  return `${WD[d.getUTCDay()]} ${MO[d.getUTCMonth()]} ${d.getUTCDate()}, ${hhmm(d)}`;
}

// Flux `aggregateWindow` aligns buckets to epoch, so at wide ranges with
// coarse buckets the first/last data points land *inside* the requested
// window. Without sentinels, the chart's time axis collapses to the data
// extent and setVisibleRange can't span the full ask. WhitespaceData is
// `{time}` with no value — it extends the time axis without drawing zeros
// (which is what crashed lightweight-charts when we tried createEmpty:true).
function padBounds(
  points: Point[],
  outerFromSec: UTCTimestamp,
  outerToSec: UTCTimestamp,
): SeriesEntry[] {
  if (points.length === 0) {
    return [{ time: outerFromSec }, { time: outerToSec }];
  }
  const out: SeriesEntry[] = [];
  const first = points[0]!.time;
  const last = points[points.length - 1]!.time;
  if (outerFromSec < first) out.push({ time: outerFromSec });
  for (const p of points) out.push(p);
  if (outerToSec > last) out.push({ time: outerToSec });
  return out;
}

function shapeData(data: SeriesPoint[]) {
  const byCat = new Map<string, Map<UTCTimestamp, number>>();
  const totalByTime = new Map<UTCTimestamp, number>();
  for (const p of data) {
    const t = toUtc(p.time);
    const kw = p.watts / 1000;
    let cat = byCat.get(p.series);
    if (!cat) {
      cat = new Map();
      byCat.set(p.series, cat);
    }
    cat.set(t, kw);
    totalByTime.set(t, (totalByTime.get(t) ?? 0) + kw);
  }
  const sortedTimes = Array.from(totalByTime.keys()).sort((a, b) => a - b);
  return { byCat, totalByTime, sortedTimes };
}

function pointsFor(
  times: UTCTimestamp[],
  source: Map<UTCTimestamp, number> | undefined,
): Point[] {
  if (!source) return [];
  return times.map((time) => ({ time, value: source.get(time) ?? 0 }));
}

function Spinner() {
  return (
    <svg
      className="h-4 w-4 animate-spin text-zinc-500 dark:text-zinc-400"
      viewBox="0 0 24 24"
      fill="none"
      aria-hidden
    >
      <circle
        cx="12"
        cy="12"
        r="10"
        stroke="currentColor"
        strokeOpacity="0.25"
        strokeWidth="3"
      />
      <path
        d="M22 12a10 10 0 0 1-10 10"
        stroke="currentColor"
        strokeWidth="3"
        strokeLinecap="round"
      />
    </svg>
  );
}

export function PowerChart({
  state,
  onVisibleChange,
}: {
  state: DashState;
  onVisibleChange: VisibleChange;
}) {
  // Latest onVisibleChange, read from the create-once chart effect without
  // re-subscribing.
  const onVisibleRef = useRef(onVisibleChange);
  onVisibleRef.current = onVisibleChange;
  const containerRef = useRef<HTMLDivElement | null>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const catSeriesRef = useRef<Map<string, ISeriesApi<"Line">>>(new Map());
  const sumSeriesRef = useRef<ISeriesApi<"Line"> | null>(null);
  const totalSeriesRef = useRef<ISeriesApi<"Line"> | null>(null);
  const gestureTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  // Last loaded payload — kept so a Show-filter toggle can recompute the Sum
  // series and per-series visibility without a refetch or a zoom reset.
  const dataRef = useRef<{
    byCat: Map<string, Map<UTCTimestamp, number>>;
    sortedTimes: UTCTimestamp[];
    outerFromSec: UTCTimestamp;
    outerToSec: UTCTimestamp;
  } | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  // Recompute the Sum series (sum of currently-selected categories) from the
  // last loaded data. No refetch, no setVisibleRange — preserves zoom.
  const applySum = (show: string[]) => {
    const d = dataRef.current;
    const sumSeries = sumSeriesRef.current;
    if (!d || !sumSeries) return;
    const selected = new Set(show);
    const showSum = selected.size >= 2;
    if (showSum) {
      const sumData: Point[] = d.sortedTimes.map((time) => {
        let v = 0;
        for (const cat of selected) v += d.byCat.get(cat)?.get(time) ?? 0;
        return { time, value: v };
      });
      try {
        sumSeries.setData(toChartData(padBounds(sumData, d.outerFromSec, d.outerToSec)));
      } catch (e) {
        console.warn("setData failed for Sum", e);
      }
    }
    sumSeries.applyOptions({ visible: showSum });
  };

  // Create chart + series once
  useEffect(() => {
    const el = containerRef.current;
    if (!el) return;

    const chart = createChart(el, {
      autoSize: true,
      layout: {
        background: { color: "transparent" },
        textColor: "#888",
      },
      grid: {
        vertLines: { color: "rgba(120,120,120,0.1)" },
        horzLines: { color: "rgba(120,120,120,0.1)" },
      },
      localization: { timeFormatter: crosshairFmt },
      rightPriceScale: { borderVisible: false },
      timeScale: {
        borderVisible: false,
        timeVisible: true,
        secondsVisible: false,
        tickMarkFormatter: tickFmt,
        // Bound pan/zoom to the loaded data: you can explore within the window
        // but can never scroll/zoom off into empty canvas (past `now`, or
        // before the first point). This is what stops the blank-out.
        fixLeftEdge: true,
        fixRightEdge: true,
        rightOffset: 0,
        lockVisibleTimeRangeOnResize: true,
      },
      handleScale: { axisPressedMouseMove: true, mouseWheel: true, pinch: true },
      handleScroll: {
        mouseWheel: true,
        pressedMouseMove: true,
        horzTouchDrag: true,
        vertTouchDrag: false,
      },
      crosshair: { mode: 1 /* Magnet */ },
    });
    chartRef.current = chart;

    const total = chart.addSeries(LineSeries, {
      color: TOTAL_COLOR,
      lineWidth: 1,
      lineStyle: LineStyle.Dotted,
      priceLineVisible: false,
      lastValueVisible: false,
    });
    totalSeriesRef.current = total;

    const sum = chart.addSeries(LineSeries, {
      color: SUM_COLOR,
      lineWidth: 2,
      priceLineVisible: false,
      lastValueVisible: false,
      visible: false,
    });
    sumSeriesRef.current = sum;

    for (const cat of CATEGORY_ORDER) {
      const s = chart.addSeries(LineSeries, {
        color: CATEGORY_COLORS[cat] ?? "#888",
        lineWidth: 2,
        priceLineVisible: false,
        lastValueVisible: false,
      });
      catSeriesRef.current.set(cat, s);
    }

    // Report the visible sub-window outward (table + header follow pan/zoom).
    // This never changes the chart's loaded window, so there's no feedback
    // loop — fixLeftEdge/fixRightEdge already keep the range inside the data.
    const onRangeChange: TimeRangeChangeEventHandler<Time> = (range) => {
      if (!range) return;
      const fromSec = Number(range.from);
      const toSec = Number(range.to);
      if (!Number.isFinite(fromSec) || !Number.isFinite(toSec)) return;
      if (gestureTimer.current) clearTimeout(gestureTimer.current);
      gestureTimer.current = setTimeout(() => {
        let from = fromDisplay(fromSec) * 1000;
        let to = fromDisplay(toSec) * 1000;
        if (!(from < to)) return;
        const now = Date.now();
        if (to > now) to = now;
        if (from < 0) from = 0;
        onVisibleRef.current(Math.round(from), Math.round(to));
      }, 180);
    };

    chart.timeScale().subscribeVisibleTimeRangeChange(onRangeChange);

    return () => {
      chart.timeScale().unsubscribeVisibleTimeRangeChange(onRangeChange);
      if (gestureTimer.current) {
        clearTimeout(gestureTimer.current);
        gestureTimer.current = null;
      }
      chart.remove();
      chartRef.current = null;
      catSeriesRef.current.clear();
      sumSeriesRef.current = null;
      totalSeriesRef.current = null;
      dataRef.current = null;
    };
    // intentionally empty: stable chart instance; callback read via onVisibleRef
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Load + render the window set by presets/bucket. Pan/zoom does NOT run this
  // (it only moves the view within the already-loaded data) — so the fetch and
  // the authoritative setVisibleRange happen only on a real window/bucket
  // change. That is what removes the old gesture feedback loop.
  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);

    const now = Date.now();
    const fromMs = state.fromMs;
    const toMs = Math.min(now, state.toMs);
    const intervalMs = intervalSeconds(state.interval) * 1000;
    const qFrom = Math.floor(fromMs / intervalMs) * intervalMs;
    const qTo = Math.floor(toMs / intervalMs) * intervalMs;

    fetchSeriesCached(qFrom, qTo, state.interval)
      .then((data) => {
        if (cancelled || !chartRef.current) return;

        const { byCat, totalByTime, sortedTimes } = shapeData(data);

        // Sentinels pin the axis to exactly [from, to] (capped at now) so the
        // visible range spans the full requested window even when bucket
        // alignment leaves the first/last point inside it.
        const outerFromSec = Math.floor(fromMs / 1000) as UTCTimestamp;
        const outerToSec = Math.floor(toMs / 1000) as UTCTimestamp;
        dataRef.current = { byCat, sortedTimes, outerFromSec, outerToSec };

        try {
          for (const cat of CATEGORY_ORDER) {
            const series = catSeriesRef.current.get(cat);
            if (!series) continue;
            const pts = padBounds(
              pointsFor(sortedTimes, byCat.get(cat)),
              outerFromSec,
              outerToSec,
            );
            try { series.setData(toChartData(pts)); }
            catch (e) { console.warn(`setData failed for ${cat}`, e); }
            const visible = state.show.length === 0 || state.show.includes(cat);
            series.applyOptions({ visible });
          }

          try {
            totalSeriesRef.current?.setData(
              toChartData(padBounds(pointsFor(sortedTimes, totalByTime), outerFromSec, outerToSec)),
            );
          } catch (e) {
            console.warn("setData failed for Total", e);
          }

          applySum(state.show);

          chartRef.current.timeScale().setVisibleRange({
            from: toDisplay(outerFromSec),
            to: toDisplay(outerToSec),
          });
        } catch (e) {
          console.error("PowerChart apply failed", e);
        } finally {
          setLoading(false);
        }
      })
      .catch((e) => {
        if (cancelled) return;
        console.error("PowerChart fetch failed:", e);
        setError(
          "Couldn't load this range — try a narrower window or coarser bucket.",
        );
        setLoading(false);
      });

    return () => {
      cancelled = true;
    };
    // applySum + state.show read fresh each run; not deps (window/bucket only).
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [state.fromMs, state.toMs, state.interval]);

  // Show-filter toggle: recompute Sum + per-series visibility from the already
  // loaded data — no refetch, no setVisibleRange, so the current zoom is kept.
  useEffect(() => {
    if (!dataRef.current) return;
    for (const cat of CATEGORY_ORDER) {
      const series = catSeriesRef.current.get(cat);
      if (!series) continue;
      series.applyOptions({
        visible: state.show.length === 0 || state.show.includes(cat),
      });
    }
    applySum(state.show);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [state.show.join(",")]);

  const legend: { label: string; color: string; dotted?: boolean }[] = [
    { label: "Total", color: TOTAL_COLOR, dotted: true },
    ...(state.show.length >= 2
      ? [{ label: "Sum", color: SUM_COLOR }]
      : []),
    ...CATEGORY_ORDER.filter(
      (c) => state.show.length === 0 || state.show.includes(c),
    ).map((c) => ({ label: c, color: CATEGORY_COLORS[c] ?? "#888" })),
  ];

  return (
    <div className="relative">
      <div
        ref={containerRef}
        className="h-[55vh] min-h-[280px] w-full touch-none sm:h-[420px]"
      />
      <div className="pointer-events-none absolute left-2 top-1 flex flex-col gap-0.5 text-[11px]">
        {legend.map((it) => (
          <div key={it.label} className="flex items-center gap-1.5">
            <span
              className="inline-block h-0 w-3.5 shrink-0"
              style={{
                borderTop: `${it.dotted ? "1px dotted" : "2px solid"} ${it.color}`,
              }}
            />
            <span className="text-zinc-500 dark:text-zinc-400">{it.label}</span>
          </div>
        ))}
      </div>
      <div className="pointer-events-none absolute right-1 top-0 text-[10px] font-medium uppercase tracking-wide text-zinc-400 dark:text-zinc-500">
        kW
      </div>
      {loading && (
        <div className="pointer-events-none absolute inset-0 flex items-center justify-center bg-zinc-50/60 backdrop-blur-[1px] dark:bg-zinc-950/60">
          <div className="flex items-center gap-2 rounded-full border border-zinc-300 bg-white/90 px-3 py-1.5 text-xs text-zinc-700 shadow-sm dark:border-zinc-700 dark:bg-zinc-900/90 dark:text-zinc-200">
            <Spinner />
            loading…
          </div>
        </div>
      )}
      {error && !loading && (
        <div className="pointer-events-none absolute inset-x-2 top-2 flex justify-center">
          <div className="rounded-md border border-red-300 bg-red-50/95 px-3 py-1.5 text-xs text-red-800 shadow-sm dark:border-red-900/60 dark:bg-red-950/80 dark:text-red-200">
            {error}
          </div>
        </div>
      )}
    </div>
  );
}
