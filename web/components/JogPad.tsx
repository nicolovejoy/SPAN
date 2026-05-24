"use client";

import { useEffect, useRef } from "react";
import type { IChartApi, UTCTimestamp } from "lightweight-charts";

// Minimum visible span when zooming in — keeps the chart from collapsing.
const MIN_SPAN_SEC = 60;
// Inertia decay factor per RAF tick (~16ms). 0.92 ≈ ~250ms to settle.
const DECAY = 0.92;
// Below this px/frame, stop the inertia loop.
const STOP_THRESHOLD = 0.4;

export function JogPad({
  chartRef,
}: {
  chartRef: React.RefObject<IChartApi | null>;
}) {
  const padRef = useRef<HTMLDivElement | null>(null);
  const stateRef = useRef({
    dragging: false,
    lastX: 0,
    lastY: 0,
    lastT: 0,
    vx: 0,
    vy: 0,
    rafId: 0 as number | 0,
  });

  // Apply a dx/dy in pad-pixel space to the chart's visible range.
  // dx → time pan (drag right = move window left = go back in time).
  // dy → zoom (drag up = wider window, drag down = narrower).
  function apply(dx: number, dy: number) {
    const chart = chartRef.current;
    const pad = padRef.current;
    if (!chart || !pad) return;
    const range = chart.timeScale().getVisibleRange();
    if (!range) return;
    const from = Number(range.from);
    const to = Number(range.to);
    if (!Number.isFinite(from) || !Number.isFinite(to) || from >= to) return;
    const span = to - from;
    const w = pad.clientWidth || 1;
    const h = pad.clientHeight || 1;

    const panSec = -(dx / w) * span;
    // Exponential zoom — drag full pad height = factor of e ≈ 2.7×.
    const zoom = Math.exp(-dy / h);
    const newSpan = Math.max(MIN_SPAN_SEC, span * zoom);

    const center = (from + to) / 2 + panSec;
    let newFrom = center - newSpan / 2;
    let newTo = center + newSpan / 2;

    // Clamp to "now" — Influx has no data past current time.
    const nowSec = Math.floor(Date.now() / 1000);
    if (newTo > nowSec) {
      const shift = newTo - nowSec;
      newTo -= shift;
      newFrom -= shift;
    }
    if (newFrom >= newTo) return;

    try {
      chart.timeScale().setVisibleRange({
        from: newFrom as UTCTimestamp,
        to: newTo as UTCTimestamp,
      });
    } catch (e) {
      console.warn("JogPad setVisibleRange failed", e);
    }
  }

  useEffect(() => {
    const pad = padRef.current;
    if (!pad) return;

    const onPointerDown = (e: PointerEvent) => {
      try { pad.setPointerCapture(e.pointerId); } catch {}
      const s = stateRef.current;
      s.dragging = true;
      s.lastX = e.clientX;
      s.lastY = e.clientY;
      s.lastT = performance.now();
      s.vx = 0;
      s.vy = 0;
      if (s.rafId) cancelAnimationFrame(s.rafId);
      s.rafId = 0;
      e.preventDefault();
    };

    const onPointerMove = (e: PointerEvent) => {
      const s = stateRef.current;
      if (!s.dragging) return;
      const dx = e.clientX - s.lastX;
      const dy = e.clientY - s.lastY;
      const t = performance.now();
      const dt = Math.max(1, t - s.lastT);
      // Convert px/ms → px/frame (assume 60fps target).
      s.vx = (dx / dt) * 16;
      s.vy = (dy / dt) * 16;
      s.lastX = e.clientX;
      s.lastY = e.clientY;
      s.lastT = t;
      apply(dx, dy);
    };

    const onPointerUp = (e: PointerEvent) => {
      const s = stateRef.current;
      if (!s.dragging) return;
      s.dragging = false;
      try { pad.releasePointerCapture(e.pointerId); } catch {}
      const tick = () => {
        s.vx *= DECAY;
        s.vy *= DECAY;
        if (Math.abs(s.vx) < STOP_THRESHOLD && Math.abs(s.vy) < STOP_THRESHOLD) {
          s.rafId = 0;
          return;
        }
        apply(s.vx, s.vy);
        s.rafId = requestAnimationFrame(tick);
      };
      s.rafId = requestAnimationFrame(tick);
    };

    pad.addEventListener("pointerdown", onPointerDown);
    pad.addEventListener("pointermove", onPointerMove);
    pad.addEventListener("pointerup", onPointerUp);
    pad.addEventListener("pointercancel", onPointerUp);
    return () => {
      pad.removeEventListener("pointerdown", onPointerDown);
      pad.removeEventListener("pointermove", onPointerMove);
      pad.removeEventListener("pointerup", onPointerUp);
      pad.removeEventListener("pointercancel", onPointerUp);
      const s = stateRef.current;
      if (s.rafId) cancelAnimationFrame(s.rafId);
    };
  }, []);

  return (
    <div className="select-none">
      <div
        ref={padRef}
        className="relative h-16 w-full touch-none cursor-grab rounded-md border border-zinc-200 bg-zinc-100 active:cursor-grabbing dark:border-zinc-800 dark:bg-zinc-900"
        aria-label="Jog pad: drag horizontally to pan time, vertically to zoom; flick for momentum"
      >
        <div className="pointer-events-none absolute left-1/2 top-0 h-full w-px bg-zinc-300/70 dark:bg-zinc-700/70" />
        <div className="pointer-events-none absolute left-0 top-1/2 h-px w-full bg-zinc-300/70 dark:bg-zinc-700/70" />
        <div className="pointer-events-none absolute inset-0 flex items-center justify-between px-3 text-[10px] uppercase tracking-wide text-zinc-400 dark:text-zinc-500">
          <span>← back</span>
          <span className="flex flex-col items-center leading-tight">
            <span>↑ zoom out</span>
            <span>↓ zoom in</span>
          </span>
          <span>fwd →</span>
        </div>
      </div>
    </div>
  );
}
