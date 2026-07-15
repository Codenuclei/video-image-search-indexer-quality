"use client";

import { useEffect, useMemo, useState } from "react";
import { Bar, BarChart, Cell, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { apiClient, type IndexStatus } from "@/lib/api";
import { Card, ServiceErrorCard, StatCard } from "@/components/ui";

const STATUS_ORDER = ["processed", "pending", "processing", "error", "skipped"] as const;

const STATUS_COLORS: Record<string, string> = {
  processed: "#22c55e",
  pending: "#eab308",
  processing: "#3b82f6",
  error: "#ef4444",
  skipped: "#71717a",
};

const STATUS_LABELS: Record<string, string> = {
  processed: "Processed",
  pending: "Pending",
  processing: "Processing",
  error: "Error",
  skipped: "Skipped",
};

export default function DashboardPage() {
  const [indexStatus, setIndexStatus] = useState<IndexStatus | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    async function load() {
      try {
        setIndexStatus(await apiClient.indexStatus());
        setError(null);
      } catch (e) {
        setError(e instanceof Error ? e.message : "Failed to load dashboard");
      }
    }
    load();
    const t = setInterval(load, 5000);
    return () => clearInterval(t);
  }, []);

  function retryLoad() {
    apiClient
      .indexStatus()
      .then((status) => {
        setIndexStatus(status);
        setError(null);
      })
      .catch((e) => setError(e instanceof Error ? e.message : "Failed to load dashboard"));
  }

  const chartData = useMemo(() => {
    const counts = indexStatus?.counts_by_status ?? {};
    const rows = Object.entries(counts).map(([status, count]) => ({
      status,
      label: STATUS_LABELS[status] ?? status,
      count,
      fill: STATUS_COLORS[status] ?? "#a855f7",
    }));
    return rows.sort(
      (a, b) =>
        (STATUS_ORDER.indexOf(a.status as (typeof STATUS_ORDER)[number]) + 1 || 99) -
        (STATUS_ORDER.indexOf(b.status as (typeof STATUS_ORDER)[number]) + 1 || 99)
    );
  }, [indexStatus]);
  const processed = indexStatus?.counts_by_status?.processed ?? 0;
  const pending = indexStatus?.counts_by_status?.pending ?? 0;
  const errors = indexStatus?.counts_by_status?.error ?? 0;

  return (
    <div className="space-y-6">
      <div>
        <h2 className="text-2xl font-semibold">Dashboard</h2>
        <p className="text-sm text-muted-foreground">Gemini Embedding 2 video search · Gemini image search · InsightFace detection</p>
      </div>

      {error && <ServiceErrorCard message={error} onRetry={retryLoad} />}

      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
        <StatCard label="Indexed" value={processed} />
        <StatCard label="Pending" value={pending} />
        <StatCard label="Errors" value={errors} />
        <StatCard
          label="Indexer"
          value={indexStatus?.is_running ? "Running" : "Idle"}
          hint={
            indexStatus?.last_run
              ? `Last: ${indexStatus.last_run.processed} processed, ${indexStatus.last_run.errored} errors`
              : undefined
          }
        />
      </div>

      <Card>
        <h3 className="mb-3 font-medium">Drive file status</h3>
        {chartData.length === 0 ? (
          <p className="text-sm text-muted-foreground">No files synced yet. Go to Folders to start indexing.</p>
        ) : (
          <>
            <div className="mb-3 flex flex-wrap gap-3 text-xs text-muted-foreground">
              {chartData.map((row) => (
                <span key={row.status} className="inline-flex items-center gap-1.5">
                  <span
                    className="inline-block h-2.5 w-2.5 rounded-sm"
                    style={{ backgroundColor: row.fill }}
                  />
                  {row.label}
                </span>
              ))}
            </div>
            <div className="h-52">
              <ResponsiveContainer width="100%" height="100%">
                <BarChart
                  data={chartData}
                  margin={{ top: 4, right: 8, left: 0, bottom: 0 }}
                  barCategoryGap="28%"
                >
                  <XAxis
                    dataKey="label"
                    stroke="var(--muted-foreground)"
                    fontSize={11}
                    tickLine={false}
                    axisLine={false}
                  />
                  <YAxis
                    stroke="var(--muted-foreground)"
                    fontSize={11}
                    allowDecimals={false}
                    tickLine={false}
                    axisLine={false}
                    width={36}
                  />
                  <Tooltip
                    cursor={{ fill: "var(--muted)", opacity: 0.35 }}
                    contentStyle={{
                      background: "var(--card)",
                      border: "1px solid var(--border)",
                      borderRadius: "var(--radius)",
                      color: "var(--card-foreground)",
                      fontSize: 12,
                    }}
                    formatter={(value: number) => [value, "Files"]}
                    labelFormatter={(label) => String(label)}
                  />
                  <Bar dataKey="count" radius={[4, 4, 0, 0]} barSize={32}>
                    {chartData.map((row) => (
                      <Cell key={row.status} fill={row.fill} />
                    ))}
                  </Bar>
                </BarChart>
              </ResponsiveContainer>
            </div>
          </>
        )}
      </Card>
    </div>
  );
}
