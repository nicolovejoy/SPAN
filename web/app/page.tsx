import { Suspense } from "react";
import { Filters } from "@/components/Filters";
import { PowerChart } from "@/components/PowerChart";
import { QuickFilters } from "@/components/QuickFilters";
import { BreakdownTable } from "@/components/BreakdownTable";
import { FocusToggle } from "@/components/FocusToggle";
import { queryEnergyByCategory } from "@/lib/influx";
import { parseState } from "@/lib/url-state";

type SP = Promise<Record<string, string | string[] | undefined>>;

export default async function Home({ searchParams }: { searchParams: SP }) {
  const params = await searchParams;
  const state = parseState(params);

  return (
    <main className="mx-auto flex w-full max-w-6xl flex-col gap-4 p-3 sm:gap-6 sm:p-6">
      <header className="focus-hide flex flex-col gap-1 sm:flex-row sm:items-baseline sm:justify-between">
        <h1 className="text-xl font-semibold tracking-tight">SPAN — power explorer</h1>
        <div className="text-xs text-zinc-500">
          {new Date(state.fromMs).toLocaleString()} → {new Date(state.toMs).toLocaleString()}
        </div>
      </header>

      <details className="focus-hide group sm:hidden">
        <summary className="cursor-pointer text-xs uppercase tracking-wide text-zinc-500">
          Filters
          <span className="ml-1 group-open:hidden">▸</span>
          <span className="ml-1 hidden group-open:inline">▾</span>
        </summary>
        <div className="mt-3">
          <Filters
            range={state.rangePreset}
            interval={state.interval}
            intervalAuto={state.intervalAuto}
            groupBy={state.groupBy}
            categories={state.categories}
          />
        </div>
      </details>

      <div className="focus-hide hidden sm:block">
        <Filters
          range={state.rangePreset}
          interval={state.interval}
          intervalAuto={state.intervalAuto}
          groupBy={state.groupBy}
          categories={state.categories}
        />
      </div>

      <div className="flex items-center justify-between gap-2">
        <div className="focus-hide min-w-0 flex-1">
          <QuickFilters show={state.show} />
        </div>
        <FocusToggle />
      </div>

      <PowerChart state={state} />

      <Suspense
        key={`${state.fromMs}-${state.toMs}-${state.categories.join(",")}-table`}
        fallback={<div className="focus-hide h-32 animate-pulse rounded-md bg-zinc-100 dark:bg-zinc-900" />}
      >
        <TablePanel state={state} />
      </Suspense>
    </main>
  );
}

async function TablePanel({ state }: { state: ReturnType<typeof parseState> }) {
  const rows = await queryEnergyByCategory({
    fromMs: state.fromMs,
    toMs: state.toMs,
    categories: state.categories.length ? state.categories : undefined,
  });
  return (
    <div className="focus-hide">
      <BreakdownTable rows={rows} />
    </div>
  );
}
