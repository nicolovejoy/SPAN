import { Suspense } from "react";
import { Filters } from "@/components/Filters";
import { PowerChart } from "@/components/PowerChart";
import { BreakdownTable } from "@/components/BreakdownTable";
import { queryEnergyByCategory, queryPower } from "@/lib/influx";
import { parseState } from "@/lib/url-state";

type SP = Promise<Record<string, string | string[] | undefined>>;

export default async function Home({ searchParams }: { searchParams: SP }) {
  const params = await searchParams;
  const state = parseState(params);

  return (
    <main className="mx-auto flex w-full max-w-6xl flex-col gap-6 p-6">
      <header className="flex items-baseline justify-between">
        <h1 className="text-xl font-semibold tracking-tight">SPAN — power explorer</h1>
        <div className="text-xs text-zinc-500">
          {new Date(state.fromMs).toLocaleString()} → {new Date(state.toMs).toLocaleString()}
        </div>
      </header>

      <Filters
        range={state.rangePreset}
        interval={state.interval}
        intervalAuto={state.intervalAuto}
        groupBy={state.groupBy}
        categories={state.categories}
      />

      <Suspense
        key={JSON.stringify(state)}
        fallback={<div className="h-[360px] animate-pulse rounded-md bg-zinc-100 dark:bg-zinc-900" />}
      >
        <ChartPanel state={state} />
      </Suspense>

      <Suspense
        key={JSON.stringify(state) + ":table"}
        fallback={<div className="h-32 animate-pulse rounded-md bg-zinc-100 dark:bg-zinc-900" />}
      >
        <TablePanel state={state} />
      </Suspense>
    </main>
  );
}

async function ChartPanel({ state }: { state: ReturnType<typeof parseState> }) {
  const data = await queryPower({
    fromMs: state.fromMs,
    toMs: state.toMs,
    interval: state.interval,
    groupBy: state.groupBy,
    categories: state.categories.length ? state.categories : undefined,
  });
  return <PowerChart data={data} />;
}

async function TablePanel({ state }: { state: ReturnType<typeof parseState> }) {
  const rows = await queryEnergyByCategory({
    fromMs: state.fromMs,
    toMs: state.toMs,
    categories: state.categories.length ? state.categories : undefined,
  });
  return <BreakdownTable rows={rows} />;
}
