"use client";

import { useEffect, useState } from "react";

export function FocusToggle() {
  const [focused, setFocused] = useState(false);

  useEffect(() => {
    document.body.classList.toggle("focus-mode", focused);
  }, [focused]);

  useEffect(() => {
    if (!focused) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setFocused(false);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [focused]);

  return (
    <button
      type="button"
      onClick={() => setFocused((f) => !f)}
      aria-pressed={focused}
      aria-label={focused ? "Exit focus mode" : "Focus chart"}
      title={focused ? "Exit focus (ESC)" : "Focus chart"}
      className="shrink-0 rounded-full border border-zinc-300 px-2.5 py-1 text-xs text-zinc-600 transition-colors hover:border-zinc-500 dark:border-zinc-700 dark:text-zinc-300"
    >
      {focused ? "✕" : "⤢"}
    </button>
  );
}
