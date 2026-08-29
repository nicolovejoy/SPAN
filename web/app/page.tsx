import { ExplorerClient } from "@/components/ExplorerClient";
import { cachedQueryEnergyByCategory } from "@/lib/queryCache";
import { buildEnergyRows, comparisonGrain, snapPeriod } from "@/lib/energyWindow";
import { parseState } from "@/lib/url-state";

type SP = Promise<Record<string, string | string[] | undefined>>;

export default async function Home({ searchParams }: { searchParams: SP }) {
  const params = await searchParams;
  const initial = parseState(params);

  // SSR the initial breakdown (the calendar period the initial window snaps
  // to, plus its prior-period comparison) so the first paint has the table;
  // the client takes over (and caches) every window after.
  const grain = comparisonGrain(initial.toMs - initial.fromMs);
  const snap = snapPeriod(initial.toMs, grain, Date.now());
  const [current, prevPeriod] = await Promise.all([
    cachedQueryEnergyByCategory({ fromMs: snap.fromMs, toMs: snap.toMs }),
    cachedQueryEnergyByCategory(snap.previous),
  ]);
  const initialEnergy = buildEnergyRows(current, prevPeriod, {
    periodFromMs: snap.fromMs,
    periodToMs: snap.toMs,
    periodGrain: grain,
    periodComplete: snap.complete,
  });

  return (
    <ExplorerClient
      initial={initial}
      initialEnergy={initialEnergy}
      buildTime={process.env.NEXT_PUBLIC_BUILD_TIME}
    />
  );
}
