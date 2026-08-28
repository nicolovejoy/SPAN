import { ExplorerClient } from "@/components/ExplorerClient";
import { cachedQueryEnergyByCategory } from "@/lib/queryCache";
import { buildEnergyRows, comparisonGrain, paceRanges } from "@/lib/energyWindow";
import { parseState } from "@/lib/url-state";

type SP = Promise<Record<string, string | string[] | undefined>>;

export default async function Home({ searchParams }: { searchParams: SP }) {
  const params = await searchParams;
  const initial = parseState(params);

  // SSR the initial breakdown (viewed window + the Δ column's calendar-pace
  // windows) so the first paint has the table; the client takes over (and
  // caches) every window after.
  const pace = paceRanges(
    initial.toMs,
    comparisonGrain(initial.toMs - initial.fromMs),
  );
  const [current, period, prevPeriod] = await Promise.all([
    cachedQueryEnergyByCategory({ fromMs: initial.fromMs, toMs: initial.toMs }),
    cachedQueryEnergyByCategory(pace.current),
    cachedQueryEnergyByCategory(pace.previous),
  ]);
  const initialEnergy = buildEnergyRows(
    current,
    period,
    prevPeriod,
    initial.toMs - initial.fromMs,
  );

  return (
    <ExplorerClient
      initial={initial}
      initialEnergy={initialEnergy}
      buildTime={process.env.NEXT_PUBLIC_BUILD_TIME}
    />
  );
}
