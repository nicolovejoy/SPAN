export function BreakdownTable({
  rows,
}: {
  rows: Array<{ category: string; kwh: number }>;
}) {
  // Only include non-negative kWh in the total — a negative would mean
  // bad upstream data and would poison the Share column.
  const total = rows.reduce((acc, r) => (r.kwh > 0 ? acc + r.kwh : acc), 0);
  return (
    <div className="overflow-hidden rounded-md border border-zinc-200 dark:border-zinc-800">
      <table className="w-full text-sm">
        <thead className="bg-zinc-50 dark:bg-zinc-900">
          <tr>
            <th className="px-3 py-2 text-left font-medium">Category</th>
            <th className="px-3 py-2 text-right font-medium">kWh</th>
            <th className="px-3 py-2 text-right font-medium">Share</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <tr key={r.category} className="border-t border-zinc-100 dark:border-zinc-800">
              <td className="px-3 py-2">{r.category}</td>
              <td className="px-3 py-2 text-right tabular-nums">{r.kwh.toFixed(1)}</td>
              <td className="px-3 py-2 text-right tabular-nums text-zinc-500">
                {total > 0 ? `${((r.kwh / total) * 100).toFixed(0)}%` : "—"}
              </td>
            </tr>
          ))}
          {rows.length === 0 && (
            <tr>
              <td colSpan={3} className="px-3 py-4 text-center text-zinc-500">
                No data in selected range.
              </td>
            </tr>
          )}
          {rows.length > 0 && (
            <tr className="border-t-2 border-zinc-300 font-medium dark:border-zinc-700">
              <td className="px-3 py-2">Total</td>
              <td className="px-3 py-2 text-right tabular-nums">{total.toFixed(1)}</td>
              <td className="px-3 py-2"></td>
            </tr>
          )}
        </tbody>
      </table>
    </div>
  );
}
