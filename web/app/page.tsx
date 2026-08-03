import { ExplorerClient } from "@/components/ExplorerClient";
import { cachedQueryEnergyByCategory } from "@/lib/queryCache";
import { buildEnergyRows, previousWindowRange } from "@/lib/energyWindow";
import { parseState } from "@/lib/url-state";

type SP = Promise<Record<string, string | string[] | undefined>>;

export default async function Home({ searchParams }: { searchParams: SP }) {
  const params = await searchParams;
  const initial = parseState(params);

  // SSR the initial breakdown (current + previous window, for the Δ column)
  // so the first paint has the table; the client takes over (and caches)
  // every window after.
  const prevRange = previousWindowRange(initial.fromMs, initial.toMs);
  const [current, previous] = await Promise.all([
    cachedQueryEnergyByCategory({ fromMs: initial.fromMs, toMs: initial.toMs }),
    cachedQueryEnergyByCategory(prevRange),
  ]);
  const initialEnergy = buildEnergyRows(
    current,
    previous,
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
