"use client";

import { useEffect, useRef, useState } from "react";
import type { IChartApi, UTCTimestamp } from "lightweight-charts";

// Minimum visible span when zooming in — keeps the chart from collapsing.
const MIN_SPAN_SEC = 60;
// Full pad width of horizontal drag = this fraction of visible span panned.
// 0.5 means dragging from left edge to right edge pans by half the visible
// span, leaving plenty of headroom for fine adjustments.
const PAN_GAIN = 0.5;
// Full pad height of vertical drag = this zoom factor.
// 1.6 means dragging the full pad-height up makes the window 1.6× wider.
const ZOOM_GAIN = 1.6;

export function JogPad({
  chartRef,
}: {
  chartRef: React.RefObject<IChartApi | null>;
}) {
  const padRef = useRef<HTMLDivElement | null>(null);
  // Cursor position relative to the pad while a drag is active.
  // null when no drag is in progress.
  const [cursor, setCursor] = useState<{ x: number; y: number } | null>(null);
  const dragRef = useRef<{
    active: boolean;
    startX: number;
    startY: number;
    // Visible range captured at gesture start — all motion is computed
    // relative to this so the pad feels like a joystick (anchored), not a
    // wheel (cumulative).
    startFromSec: number;
    startToSec: number;
  }>({ active: false, startX: 0, startY: 0, startFromSec: 0, startToSec: 0 });

  function applyAbsolute(dx: number, dy: number) {
    const chart = chartRef.current;
    const pad = padRef.current;
    if (!chart || !pad) return;
    const w = pad.clientWidth || 1;
    const h = pad.clientHeight || 1;

    const { startFromSec, startToSec } = dragRef.current;
    if (!(startFromSec < startToSec)) return;
    const startSpan = startToSec - startFromSec;

    // Drag right → window slides left in time (you swipe content to the past).
    const panSec = -(dx / w) * startSpan * PAN_GAIN;
    // Drag up → wider window (zoom out).
    const zoom = Math.pow(ZOOM_GAIN, -dy / h);
    const newSpan = Math.max(MIN_SPAN_SEC, startSpan * zoom);

    const startCenter = (startFromSec + startToSec) / 2;
    const newCenter = startCenter + panSec;
    let newFrom = newCenter - newSpan / 2;
    let newTo = newCenter + newSpan / 2;

    const nowSec = Math.floor(Date.now() / 1000);
    if (newTo > nowSec) {
      const shift = newTo - nowSec;
      newTo -= shift;
      newFrom -= shift;
    }
    if (!(newFrom < newTo)) return;

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

    const localCoords = (e: PointerEvent) => {
      const rect = pad.getBoundingClientRect();
      return { x: e.clientX - rect.left, y: e.clientY - rect.top };
    };

    const onPointerDown = (e: PointerEvent) => {
      const chart = chartRef.current;
      if (!chart) return;
      const range = chart.timeScale().getVisibleRange();
      if (!range) return;
      const from = Number(range.from);
      const to = Number(range.to);
      if (!Number.isFinite(from) || !Number.isFinite(to) || from >= to) return;
      try { pad.setPointerCapture(e.pointerId); } catch {}
      dragRef.current = {
        active: true,
        startX: e.clientX,
        startY: e.clientY,
        startFromSec: from,
        startToSec: to,
      };
      setCursor(localCoords(e));
      e.preventDefault();
    };

    const onPointerMove = (e: PointerEvent) => {
      const d = dragRef.current;
      if (!d.active) return;
      const dx = e.clientX - d.startX;
      const dy = e.clientY - d.startY;
      setCursor(localCoords(e));
      applyAbsolute(dx, dy);
    };

    const onPointerUp = (e: PointerEvent) => {
      const d = dragRef.current;
      if (!d.active) return;
      d.active = false;
      try { pad.releasePointerCapture(e.pointerId); } catch {}
      setCursor(null);
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
    };
  }, []);

  return (
    <div className="select-none">
      <div
        ref={padRef}
        className="relative h-24 w-full touch-none cursor-grab rounded-md border border-zinc-300 bg-zinc-100 active:cursor-grabbing dark:border-zinc-700 dark:bg-zinc-900"
        aria-label="Jog pad: drag horizontally to pan time, vertically to zoom"
      >
        <div className="pointer-events-none absolute left-1/2 top-0 h-full w-px bg-zinc-300 dark:bg-zinc-700" />
        <div className="pointer-events-none absolute left-0 top-1/2 h-px w-full bg-zinc-300 dark:bg-zinc-700" />
        <div className="pointer-events-none absolute inset-x-0 top-1 flex justify-center text-[10px] uppercase tracking-wide text-zinc-400 dark:text-zinc-500">
          ↑ zoom out
        </div>
        <div className="pointer-events-none absolute inset-x-0 bottom-1 flex justify-center text-[10px] uppercase tracking-wide text-zinc-400 dark:text-zinc-500">
          ↓ zoom in
        </div>
        <div className="pointer-events-none absolute inset-y-0 left-2 flex items-center text-[10px] uppercase tracking-wide text-zinc-400 dark:text-zinc-500">
          ← back
        </div>
        <div className="pointer-events-none absolute inset-y-0 right-2 flex items-center text-[10px] uppercase tracking-wide text-zinc-400 dark:text-zinc-500">
          fwd →
        </div>
        {cursor && (
          <div
            className="pointer-events-none absolute h-6 w-6 -translate-x-1/2 -translate-y-1/2 rounded-full border-2 border-zinc-900 bg-zinc-900/20 dark:border-zinc-100 dark:bg-zinc-100/20"
            style={{ left: cursor.x, top: cursor.y }}
          />
        )}
      </div>
    </div>
  );
}
