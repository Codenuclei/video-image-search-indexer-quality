"use client";

import { useEffect, useState } from "react";
import { Bar, BarChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { apiClient, type IndexStatus } from "@/lib/api";
import { Card, StatCard } from "@/components/ui";

export default function DashboardPage() {
  const [indexStatus, setIndexStatus] = useState<IndexStatus | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    async function load() {
      try {
        setIndexStatus(await apiClient.indexStatus());
      } catch (e) {
        setError(e instanceof Error ? e.message : "Failed to load dashboard");
      }
    }
    load();
    const t = setInterval(load, 5000);
    return () => clearInterval(t);
  }, []);

  const chartData = Object.entries(indexStatus?.counts_by_status ?? {}).map(([status, count]) => ({
    status,
    count,
  }));
  const processed = indexStatus?.counts_by_status?.processed ?? 0;
  const pending = indexStatus?.counts_by_status?.pending ?? 0;
  const errors = indexStatus?.counts_by_status?.error ?? 0;

  return (
    <div className="space-y-6">
      <div>
        <h2 className="text-2xl font-semibold">Dashboard</h2>
        <p className="text-sm text-muted-foreground">Gemini Embedding 2 video search · Gemini image search · InsightFace detection</p>
      </div>

      {error && <Card className="border-destructive text-destructive">{error}</Card>}

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
        <h3 className="mb-4 font-medium">Drive file status</h3>
        {chartData.length === 0 ? (
          <p className="text-sm text-muted-foreground">No files synced yet. Go to Folders to start indexing.</p>
        ) : (
          <div className="h-64">
            <ResponsiveContainer width="100%" height="100%">
              <BarChart data={chartData}>
                <XAxis dataKey="status" stroke="var(--muted-foreground)" fontSize={12} />
                <YAxis stroke="var(--muted-foreground)" fontSize={12} allowDecimals={false} />
                <Tooltip
                  contentStyle={{
                    background: "var(--card)",
                    border: "1px solid var(--border)",
                    borderRadius: "var(--radius)",
                    color: "var(--card-foreground)",
                    fontSize: 12,
                  }}
                />
                <Bar dataKey="count" fill="var(--primary)" radius={[4, 4, 0, 0]} />
              </BarChart>
            </ResponsiveContainer>
          </div>
        )}
      </Card>
    </div>
  );
}
