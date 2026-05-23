"use client";

import { useRouter, useSearchParams } from "next/navigation";
import { useEffect, useRef, useState } from "react";
import {
  createChart,
  LineSeries,
  LineStyle,
  type IChartApi,
  type ISeriesApi,
  type TimeRangeChangeEventHandler,
  type Time,
  type UTCTimestamp,
} from "lightweight-charts";
import { autoInterval } from "@/lib/interval";
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

type Point = { time: UTCTimestamp; value: number };

function toUtc(iso: string): UTCTimestamp {
  return Math.floor(new Date(iso).getTime() / 1000) as UTCTimestamp;
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
  const [loading, setLoading] = useState(true);

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
      rightPriceScale: { borderVisible: false },
      timeScale: {
        borderVisible: false,
        timeVisible: true,
        secondsVisible: false,
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
      title: "Total",
    });
    totalSeriesRef.current = total;

    const sum = chart.addSeries(LineSeries, {
      color: SUM_COLOR,
      lineWidth: 2,
      priceLineVisible: false,
      lastValueVisible: false,
      visible: false,
      title: "Sum",
    });
    sumSeriesRef.current = sum;

    for (const cat of CATEGORY_ORDER) {
      const s = chart.addSeries(LineSeries, {
        color: CATEGORY_COLORS[cat] ?? "#888",
        lineWidth: 2,
        priceLineVisible: false,
        lastValueVisible: false,
        title: cat,
      });
      catSeriesRef.current.set(cat, s);
    }

    const onRangeChange: TimeRangeChangeEventHandler<Time> = (range) => {
      if (!range || externalUpdate.current) return;
      // Range times are UTCTimestamp (seconds) for our number-time series.
      const fromSec = Number(range.from);
      const toSec = Number(range.to);
      if (!Number.isFinite(fromSec) || !Number.isFinite(toSec)) return;
      if (gestureTimer.current) clearTimeout(gestureTimer.current);
      gestureTimer.current = setTimeout(() => {
        const from = fromSec * 1000;
        const to = toSec * 1000;
        if (!(from < to)) return;
        // Suppress sub-2% wobble — lightweight-charts emits range-change events
        // after our own setVisibleRange settle that differ slightly due to
        // bar-snap; without this we get a feedback loop of URL self-updates.
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
    // intentionally empty: we want a stable chart instance; router/params
    // are read via ref-like closures at fire time via useSearchParams() reactivity
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Fetch + apply data whenever the relevant state changes
  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    const url = new URL("/api/power", window.location.origin);
    url.searchParams.set("from", String(state.fromMs));
    url.searchParams.set("to", String(state.toMs));
    url.searchParams.set("interval", state.interval);
    url.searchParams.set("groupBy", "category");
    if (state.categories.length) {
      url.searchParams.set("categories", state.categories.join(","));
    }

    fetch(url.toString())
      .then((r) => r.json())
      .then((json: { data: SeriesPoint[] }) => {
        if (cancelled || !chartRef.current) return;
        const { byCat, totalByTime, sortedTimes } = shapeData(json.data);

        externalUpdate.current = true;
        try {
          for (const cat of CATEGORY_ORDER) {
            const series = catSeriesRef.current.get(cat);
            if (!series) continue;
            series.setData(pointsFor(sortedTimes, byCat.get(cat)));
            const visible = state.show.length === 0 || state.show.includes(cat);
            series.applyOptions({ visible });
          }

          totalSeriesRef.current?.setData(pointsFor(sortedTimes, totalByTime));

          const selected = new Set(state.show);
          const showSum = selected.size >= 2;
          if (showSum) {
            const sumData: Point[] = sortedTimes.map((time) => {
              let v = 0;
              for (const cat of selected) v += byCat.get(cat)?.get(time) ?? 0;
              return { time, value: v };
            });
            sumSeriesRef.current?.setData(sumData);
          }
          sumSeriesRef.current?.applyOptions({ visible: showSum });

          chartRef.current.timeScale().setVisibleRange({
            from: ((state.fromMs / 1000) | 0) as UTCTimestamp,
            to: ((state.toMs / 1000) | 0) as UTCTimestamp,
          });
          lastPushedRef.current = { from: state.fromMs, to: state.toMs };
        } finally {
          setLoading(false);
          // Hold for 150ms — gives lightweight-charts enough time to emit
          // its post-setVisibleRange snap-aligned range-change without us
          // mistaking it for a real gesture.
          setTimeout(() => {
            externalUpdate.current = false;
          }, 150);
        }
      })
      .catch((e) => {
        if (cancelled) return;
        console.error("PowerChart fetch failed:", e);
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
    state.categories.join(","),
    state.show.join(","),
  ]);

  return (
    <div className="relative">
      <div
        ref={containerRef}
        className="h-[420px] w-full touch-pan-y"
      />
      {loading && (
        <div className="pointer-events-none absolute inset-0 flex items-center justify-center text-xs text-zinc-500">
          loading…
        </div>
      )}
    </div>
  );
}
