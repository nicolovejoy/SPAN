import { ExplorerClient } from "@/components/ExplorerClient";
import { cachedQueryEnergyByCategory } from "@/lib/queryCache";
import { parseState } from "@/lib/url-state";

type SP = Promise<Record<string, string | string[] | undefined>>;

export default async function Home({ searchParams }: { searchParams: SP }) {
  const params = await searchParams;
  const initial = parseState(params);

  // SSR the initial breakdown so the first paint has the table; the client
  // takes over (and caches) every window after.
  const initialEnergy = await cachedQueryEnergyByCategory({
    fromMs: initial.fromMs,
    toMs: initial.toMs,
  });

  return (
    <ExplorerClient
      initial={initial}
      initialEnergy={initialEnergy}
      buildTime={process.env.NEXT_PUBLIC_BUILD_TIME}
    />
  );
}
