"use client";

import { useEffect, useReducer, useRef, useState } from "react";
import { PowerChart, type ViewChange } from "./PowerChart";
import { TimeNav } from "./TimeNav";
import { BucketSelector, type BucketKey } from "./BucketSelector";
import { QuickFilters } from "./QuickFilters";
import { FocusToggle } from "./FocusToggle";
import { BreakdownTable } from "./BreakdownTable";
import {
  autoInterval,
  RANGE_PRESETS,
  type IntervalKey,
  type RangePreset,
} from "@/lib/interval";
import { buildIntentSearch, type DashState } from "@/lib/url-state";
import { fetchEnergyCached, seedEnergy } from "@/lib/clientFetch";
import type { EnergyRow } from "@/lib/queryCache";

// Header range label — pinned to Pacific so server and client render the same
// string (no hydration mismatch) and it matches the chart axis.
const headerFmt = (ms: number) =>
  new Intl.DateTimeFormat("en-US", {
    timeZone: "America/Los_Angeles",
    month: "numeric",
    day: "numeric",
    year: "numeric",
    hour: "numeric",
    minute: "2-digit",
  }).format(new Date(ms));

type Action =
  | { type: "preset"; preset: RangePreset; now: number }
  | { type: "bucket"; key: BucketKey }
  | { type: "show"; show: string[] }
  | { type: "view"; fromMs: number; toMs: number; interval: IntervalKey };

function reducer(s: DashState, a: Action): DashState {
  switch (a.type) {
    case "preset": {
      const fromMs = a.now - RANGE_PRESETS[a.preset];
      return {
        ...s,
        fromMs,
        toMs: a.now,
        rangePreset: a.preset,
        interval: autoInterval(fromMs, a.now),
        intervalAuto: true,
      };
    }
    case "bucket":
      return a.key === "auto"
        ? { ...s, interval: autoInterval(s.fromMs, s.toMs), intervalAuto: true }
        : { ...s, interval: a.key, intervalAuto: false };
    case "show":
      return { ...s, show: a.show };
    case "view":
      // A pan/zoom drops the preset and re-derives the auto bucket.
      return {
        ...s,
        fromMs: a.fromMs,
        toMs: a.toMs,
        interval: a.interval,
        intervalAuto: true,
        rangePreset: null,
      };
  }
}

export function ExplorerClient({
  initial,
  initialEnergy,
  buildTime,
}: {
  initial: DashState;
  initialEnergy: EnergyRow[];
  buildTime?: string;
}) {
  const [view, dispatch] = useReducer(reducer, initial);

  // Seed the energy cache once with the SSR rows so the first table render needs
  // no client round-trip.
  const seeded = useRef(false);
  if (!seeded.current) {
    seedEnergy(initial.fromMs, initial.toMs, initialEnergy);
    seeded.current = true;
  }

  // Intent-only URL — preset + filter, never the pan/zoom window. replaceState
  // (not router) so there's no server navigation.
  useEffect(() => {
    const search = buildIntentSearch(view.rangePreset, view.show);
    window.history.replaceState(null, "", search ? `/?${search}` : "/");
  }, [view.rangePreset, view.show]);

  // Breakdown table — cache-backed energy fetch for the current window.
  const [rows, setRows] = useState<EnergyRow[]>(initialEnergy);
  const [tableLoading, setTableLoading] = useState(false);
  useEffect(() => {
    let cancelled = false;
    setTableLoading(true);
    fetchEnergyCached(view.fromMs, view.toMs)
      .then((r) => {
        if (!cancelled) setRows(r);
      })
      .catch(() => {
        if (!cancelled) setRows([]);
      })
      .finally(() => {
        if (!cancelled) setTableLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [view.fromMs, view.toMs]);

  const filtered =
    view.show.length === 0
      ? rows
      : rows.filter((r) => view.show.includes(r.category));

  const onView: ViewChange = (fromMs, toMs, interval) =>
    dispatch({ type: "view", fromMs, toMs, interval });

  return (
    <main className="mx-auto flex w-full max-w-6xl flex-col gap-4 p-3 sm:gap-6 sm:p-6">
      <header className="focus-hide flex flex-col gap-1 sm:flex-row sm:items-baseline sm:justify-between">
        <h1 className="text-xl font-semibold tracking-tight">SPAN — power explorer</h1>
        <div className="flex flex-col text-xs text-zinc-500 sm:items-end">
          <div>
            {headerFmt(view.fromMs)} → {headerFmt(view.toMs)}
          </div>
          {buildTime && (
            <div className="text-[10px] text-zinc-400">build {buildTime} PT</div>
          )}
        </div>
      </header>

      <div className="focus-hide">
        <TimeNav
          range={view.rangePreset}
          fromMs={view.fromMs}
          toMs={view.toMs}
          onPreset={(preset) =>
            dispatch({ type: "preset", preset, now: Date.now() })
          }
        />
      </div>

      <div className="focus-hide">
        <BucketSelector
          interval={view.interval}
          intervalAuto={view.intervalAuto}
          fromMs={view.fromMs}
          toMs={view.toMs}
          onSelect={(key) => dispatch({ type: "bucket", key })}
        />
      </div>

      <div className="flex items-center justify-between gap-2">
        <div className="focus-hide min-w-0 flex-1">
          <QuickFilters
            show={view.show}
            onChange={(show) => dispatch({ type: "show", show })}
          />
        </div>
        <FocusToggle />
      </div>

      <PowerChart state={view} onView={onView} />

      <div className="focus-hide">
        {tableLoading && rows.length === 0 ? (
          <div className="h-32 animate-pulse rounded-md bg-zinc-100 dark:bg-zinc-900" />
        ) : (
          <BreakdownTable rows={filtered} />
        )}
      </div>
    </main>
  );
}
