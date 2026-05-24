"use client";

import { useRouter, useSearchParams } from "next/navigation";
import { useCallback, useTransition } from "react";

/**
 * Returns an `update` fn that merges a patch into the current URL search
 * params and `router.replace`s under a transition. Null values delete the key.
 * Centralizes the "build new URLSearchParams + router.replace" boilerplate.
 */
export function useUpdateParams() {
  const router = useRouter();
  const params = useSearchParams();
  const [pending, startTransition] = useTransition();

  const update = useCallback(
    (patch: Record<string, string | null>) => {
      const next = new URLSearchParams(params.toString());
      for (const [k, v] of Object.entries(patch)) {
        if (v === null) next.delete(k);
        else next.set(k, v);
      }
      startTransition(() => {
        router.replace(`/?${next.toString()}`);
      });
    },
    [router, params],
  );

  return { update, pending };
}
