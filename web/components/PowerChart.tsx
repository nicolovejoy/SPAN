"use client";

import {
  AreaChart,
  Area,
  XAxis,
  YAxis,
  Tooltip,
  ResponsiveContainer,
  CartesianGrid,
  Legend,
} from "recharts";
import type { SeriesPoint } from "@/lib/influx";

const CATEGORY_COLORS: Record<string, string> = {
  HVAC: "#ef4444",
  EV: "#3b82f6",
  Kitchen: "#f59e0b",
  Laundry: "#a855f7",
  Lights: "#eab308",
  Other: "#6b7280",
  Total: "#0ea5e9",
};

function pickColor(name: string, idx: number) {
  if (CATEGORY_COLORS[name]) return CATEGORY_COLORS[name];
  const palette = ["#10b981", "#06b6d4", "#8b5cf6", "#f43f5e", "#84cc16", "#ec4899"];
  return palette[idx % palette.length];
}

export function PowerChart({ data }: { data: SeriesPoint[] }) {
  const seriesNames = Array.from(new Set(data.map((d) => d.series)));
  const byTime = new Map<string, Record<string, number>>();
  for (const p of data) {
    const row = byTime.get(p.time) ?? { time: 0 };
    row[p.series] = p.watts / 1000; // kW
    byTime.set(p.time, row as Record<string, number>);
  }
  const rows = Array.from(byTime.entries())
    .map(([time, vals]) => ({ time, ...vals }))
    .sort((a, b) => a.time.localeCompare(b.time));

  return (
    <div className="h-[360px] w-full">
      <ResponsiveContainer>
        <AreaChart data={rows} margin={{ top: 8, right: 12, left: 0, bottom: 0 }}>
          <CartesianGrid strokeDasharray="3 3" stroke="rgba(120,120,120,0.15)" />
          <XAxis
            dataKey="time"
            tickFormatter={(v) => formatTick(v)}
            stroke="currentColor"
            fontSize={11}
            minTickGap={32}
          />
          <YAxis
            stroke="currentColor"
            fontSize={11}
            tickFormatter={(v) => `${v.toFixed(1)} kW`}
          />
          <Tooltip
            labelFormatter={(v) => new Date(v as string).toLocaleString()}
            formatter={(v) => [`${Number(v).toFixed(2)} kW`, ""]}
            contentStyle={{ background: "rgba(20,20,20,0.92)", border: "none", color: "white" }}
          />
          <Legend />
          {seriesNames.map((name, i) => (
            <Area
              key={name}
              type="monotone"
              dataKey={name}
              stackId="1"
              stroke={pickColor(name, i)}
              fill={pickColor(name, i)}
              fillOpacity={0.55}
            />
          ))}
        </AreaChart>
      </ResponsiveContainer>
    </div>
  );
}

function formatTick(iso: string) {
  const d = new Date(iso);
  const today = new Date();
  if (d.toDateString() === today.toDateString()) {
    return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  }
  return d.toLocaleDateString([], { month: "short", day: "numeric" });
}
