"use client";

import { useEffect, useMemo, useState } from "react";
import { Bar, BarChart, Cell, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { apiClient, type IndexStatus } from "@/lib/api";
import { Card, LoadingLabel, ServiceErrorCard, StatCard } from "@/components/ui";

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

      {error && (
        <ServiceErrorCard
          message={error}
          onRetry={retryLoad}
          onDismiss={() => setError(null)}
        />
      )}

      {!indexStatus && !error && (
        <p className="text-sm text-muted-foreground">
          <LoadingLabel size={16}>Loading dashboard…</LoadingLabel>
        </p>
      )}

      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
        <StatCard label="Indexed" value={processed} />
        <StatCard label="Pending" value={pending} />
        <StatCard label="Errors" value={errors} />
        <StatCard
          label="Indexer"
          value={
            !indexStatus ? "…" : indexStatus.is_running ? (
              <LoadingLabel size={18}>Running</LoadingLabel>
            ) : (
              "Idle"
            )
          }
          hint={
            indexStatus?.last_run
              ? `Last: ${indexStatus.last_run.processed} processed, ${indexStatus.last_run.errored} errors`
              : undefined
          }
        />
      </div>

      <Card>
        <h3 className="mb-3 font-medium">Drive file status</h3>
        {!indexStatus ? (
          <p className="text-sm text-muted-foreground">
            <LoadingLabel size={14}>Loading chart…</LoadingLabel>
          </p>
        ) : chartData.length === 0 ? (
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
            <div className="h-44 max-w-md">
              <ResponsiveContainer width="100%" height="100%">
                <BarChart
                  data={chartData}
                  margin={{ top: 8, right: 4, left: -8, bottom: 0 }}
                  barCategoryGap="12%"
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
                    width={32}
                  />
                  <Tooltip
                    cursor={{ fill: "transparent" }}
                    contentStyle={{
                      background: "#0f172a",
                      border: "1px solid #475569",
                      borderRadius: 8,
                      color: "#f8fafc",
                      fontSize: 12,
                      boxShadow: "0 8px 24px rgba(0,0,0,0.35)",
                      padding: "8px 12px",
                    }}
                    itemStyle={{ color: "#f8fafc", fontWeight: 500 }}
                    labelStyle={{ color: "#f8fafc", fontWeight: 700, marginBottom: 4 }}
                    formatter={(value: number) => [String(value), "Files"]}
                    labelFormatter={(label) => String(label)}
                  />
                  <Bar dataKey="count" radius={[4, 4, 0, 0]} barSize={40} maxBarSize={48}>
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
