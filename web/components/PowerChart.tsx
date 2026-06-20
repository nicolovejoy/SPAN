"use client";

import { useRouter, useSearchParams } from "next/navigation";
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
import { autoInterval, intervalSeconds, type IntervalKey } from "@/lib/interval";
import type { SeriesPoint } from "@/lib/influx";
import type { DashState } from "@/lib/url-state";

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

// Buffered fetch — load extra data on each side of the visible window so
// small pan/zoom gestures stay within loaded data and don't refetch.
// Additive (not multiplicative). Beyond 7d the user is in overview mode
// and won't pinch much, so skip the buffer to keep Influx queries fast.
const BUFFER_MS = 1 * 24 * 60 * 60 * 1000; // 1 day
const SKIP_BUFFER_ABOVE_MS = 7 * 24 * 60 * 60 * 1000; // 7 days
function bufferFor(spanMs: number): number {
  if (spanMs > SKIP_BUFFER_ABOVE_MS) return 0;
  return Math.min(BUFFER_MS, spanMs);
}

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

export function PowerChart({ state }: { state: DashState }) {
  const router = useRouter();
  const params = useSearchParams();
  const containerRef = useRef<HTMLDivElement | null>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const catSeriesRef = useRef<Map<string, ISeriesApi<"Line">>>(new Map());
  const sumSeriesRef = useRef<ISeriesApi<"Line"> | null>(null);
  const totalSeriesRef = useRef<ISeriesApi<"Line"> | null>(null);
  const gestureTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const externalUpdate = useRef(false);
  const lastPushedRef = useRef<{ from: number; to: number }>({ from: 0, to: 0 });
  // What's currently in chart memory — used to skip refetch when a pan/zoom
  // stays within the loaded buffer.
  const loadedRef = useRef<{
    fromSec: number;
    toSec: number;
    interval: IntervalKey;
  } | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

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

    const onRangeChange: TimeRangeChangeEventHandler<Time> = (range) => {
      if (!range || externalUpdate.current) return;
      const fromSec = Number(range.from);
      const toSec = Number(range.to);
      if (!Number.isFinite(fromSec) || !Number.isFinite(toSec)) return;
      if (gestureTimer.current) clearTimeout(gestureTimer.current);
      gestureTimer.current = setTimeout(() => {
        let from = fromDisplay(fromSec) * 1000;
        let to = fromDisplay(toSec) * 1000;
        if (!(from < to)) return;
        // Don't push URLs into the future — Influx returns no data past now.
        const now = Date.now();
        if (to > now) {
          const span = to - from;
          to = now;
          from = Math.max(0, now - span);
        }
        // Suppress sub-2% wobble — lightweight-charts emits range-change events
        // after our own setVisibleRange settle that differ slightly.
        const span = to - from;
        const last = lastPushedRef.current;
        if (last.from > 0 && last.to > 0) {
          const fromDelta = Math.abs(from - last.from) / span;
          const toDelta = Math.abs(to - last.to) / span;
          const spanDelta = Math.abs(span - (last.to - last.from)) / span;
          if (fromDelta < 0.02 && toDelta < 0.02 && spanDelta < 0.02) return;
        }
        lastPushedRef.current = { from, to };
        const newInterval = autoInterval(from, to);
        const next = new URLSearchParams(params.toString());
        next.delete("range");
        next.set("from", String(Math.round(from)));
        next.set("to", String(Math.round(to)));
        next.set("interval", newInterval);
        router.replace(`/?${next.toString()}`);
      }, 220);
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
    };
    // intentionally empty: stable chart instance; router/params read via closures
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Fetch + apply data, OR fast-path: if the request fits inside loaded data
  // with the same bucket + filters, just call setVisibleRange. This is what
  // makes pinch/jog-pad feel instant — only crossing the loaded extent or
  // changing bucket triggers an actual Influx round-trip.
  useEffect(() => {
    const wantFromSec = (state.fromMs / 1000) | 0;
    const wantToSec = (state.toMs / 1000) | 0;

    const loaded = loadedRef.current;
    const fitsBuffer =
      loaded &&
      loaded.interval === state.interval &&
      wantFromSec >= loaded.fromSec &&
      wantToSec <= loaded.toSec;

    if (fitsBuffer && chartRef.current) {
      externalUpdate.current = true;
      try {
        chartRef.current.timeScale().setVisibleRange({
          from: toDisplay(wantFromSec),
          to: toDisplay(wantToSec),
        });
      } catch (e) {
        console.warn("setVisibleRange (fast path) failed", e);
      }
      // Also reapply per-series visibility — state.show may have changed.
      for (const cat of CATEGORY_ORDER) {
        const series = catSeriesRef.current.get(cat);
        if (!series) continue;
        const visible = state.show.length === 0 || state.show.includes(cat);
        series.applyOptions({ visible });
      }
      sumSeriesRef.current?.applyOptions({ visible: state.show.length >= 2 });
      lastPushedRef.current = { from: state.fromMs, to: state.toMs };
      setTimeout(() => { externalUpdate.current = false; }, 150);
      return;
    }

    // Slow path — fetch with an additive buffer (capped at BUFFER_MS / span).
    let cancelled = false;
    setLoading(true);
    setError(null);
    const span = state.toMs - state.fromMs;
    const now = Date.now();
    const pad = bufferFor(span);
    const fetchFromMs = Math.max(0, state.fromMs - pad);
    const fetchToMs = Math.min(now, state.toMs + pad);

    // Quantize to interval boundary so consecutive loads of the same view
    // produce byte-identical URLs and hit the browser's HTTP cache. The
    // chart's time axis still extends to the unquantized fetchToMs via the
    // whitespace sentinels, so the visible window remains accurate.
    const intervalMs = intervalSeconds(state.interval) * 1000;
    const qFetchFromMs = Math.floor(fetchFromMs / intervalMs) * intervalMs;
    const qFetchToMs = Math.floor(fetchToMs / intervalMs) * intervalMs;

    const url = new URL("/api/power", window.location.origin);
    url.searchParams.set("from", String(qFetchFromMs));
    url.searchParams.set("to", String(qFetchToMs));
    url.searchParams.set("interval", state.interval);

    fetch(url.toString())
      .then(async (r) => {
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        return r.json() as Promise<{ data: SeriesPoint[] }>;
      })
      .then((json: { data: SeriesPoint[] }) => {
        if (cancelled || !chartRef.current) return;

        if (!json.data || json.data.length === 0) {
          setLoading(false);
          return;
        }

        const { byCat, totalByTime, sortedTimes } = shapeData(json.data);
        if (sortedTimes.length === 0) {
          setLoading(false);
          return;
        }

        // Sentinel bounds — extend the time axis to the full fetched range so
        // bucket-alignment gaps at wide ranges don't crop the visible window,
        // and pans within the buffer can fast-path setVisibleRange.
        const outerFromSec = Math.floor(fetchFromMs / 1000) as UTCTimestamp;
        const outerToSec = Math.floor(fetchToMs / 1000) as UTCTimestamp;

        externalUpdate.current = true;
        try {
          for (const cat of CATEGORY_ORDER) {
            const series = catSeriesRef.current.get(cat);
            if (!series) continue;
            const data = padBounds(
              pointsFor(sortedTimes, byCat.get(cat)),
              outerFromSec,
              outerToSec,
            );
            try { series.setData(toChartData(data)); }
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

          const selected = new Set(state.show);
          const showSum = selected.size >= 2;
          if (showSum) {
            const sumData: Point[] = sortedTimes.map((time) => {
              let v = 0;
              for (const cat of selected) v += byCat.get(cat)?.get(time) ?? 0;
              return { time, value: v };
            });
            try {
              sumSeriesRef.current?.setData(
                toChartData(padBounds(sumData, outerFromSec, outerToSec)),
              );
            } catch (e) { console.warn("setData failed for Sum", e); }
          }
          sumSeriesRef.current?.applyOptions({ visible: showSum });

          chartRef.current.timeScale().setVisibleRange({
            from: toDisplay(wantFromSec),
            to: toDisplay(wantToSec),
          });
          loadedRef.current = {
            fromSec: outerFromSec,
            toSec: outerToSec,
            interval: state.interval,
          };
          lastPushedRef.current = { from: state.fromMs, to: state.toMs };
        } catch (e) {
          console.error("PowerChart apply failed", e);
        } finally {
          setLoading(false);
          setTimeout(() => {
            externalUpdate.current = false;
          }, 150);
        }
      })
      .catch((e) => {
        if (cancelled) return;
        console.error("PowerChart fetch failed:", e);
        setError(
          "Couldn't load this range — try a narrower window or coarser bucket.",
        );
        setLoading(false);
        externalUpdate.current = false;
      });

    return () => {
      cancelled = true;
    };
  }, [
    state.fromMs,
    state.toMs,
    state.interval,
    state.show.join(","),
  ]);

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
