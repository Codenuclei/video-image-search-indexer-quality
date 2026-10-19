"use client";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { useAuthSession } from "@/components/auth-gate";
import { Button, Card, Input, LoadingLabel } from "@/components/ui";
import {
  formatApiError,
  listSearchAudits,
  type SearchAuditSummary,
} from "@/lib/api";

const PAGE_SIZE = 25;

function formatTat(ms: number): string {
  if (!ms || ms <= 0) return "—";
  if (ms < 1000) return `${ms}ms`;
  const sec = ms / 1000;
  if (sec < 60) return `${sec < 10 ? sec.toFixed(1) : Math.round(sec)}s`;
  return `${(sec / 60).toFixed(1)}m`;
}

function formatWhen(iso: string | null | undefined): string {
  if (!iso) return "—";
  try {
    return new Date(iso).toLocaleString();
  } catch {
    return iso;
  }
}

export default function SearchAuditListPage() {
  const router = useRouter();
  const { isAdmin: allowed } = useAuthSession();
  const [items, setItems] = useState<SearchAuditSummary[]>([]);
  const [total, setTotal] = useState(0);
  const [offset, setOffset] = useState(0);
  const [q, setQ] = useState("");
  const [status, setStatus] = useState("");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!allowed) router.replace("/");
  }, [allowed, router]);

  const load = useCallback(async () => {
    if (!allowed) return;
    setLoading(true);
    setError(null);
    try {
      const data = await listSearchAudits({
        limit: PAGE_SIZE,
        offset,
        q: q.trim() || undefined,
        status: status.trim() || undefined,
      });
      setItems(data.items);
      setTotal(data.total);
    } catch (err) {
      setError(formatApiError(err, "Could not load search audits"));
      setItems([]);
      setTotal(0);
    } finally {
      setLoading(false);
    }
  }, [allowed, offset, q, status]);

  useEffect(() => {
    void load();
  }, [load]);

  if (!allowed) return null;

  const page = Math.floor(offset / PAGE_SIZE) + 1;
  const pageCount = Math.max(1, Math.ceil(total / PAGE_SIZE));

  return (
    <div className="mx-auto max-w-6xl space-y-4 p-4 sm:p-6">
      <div>
        <h1 className="text-2xl font-semibold text-foreground">Search audit</h1>
        <p className="mt-1 text-sm text-muted-foreground">
          Recent production search requests with timing, status, and stored result snapshots.
        </p>
      </div>

      <Card className="flex flex-col gap-3 sm:flex-row sm:items-end">
        <label className="block min-w-0 flex-1 space-y-1">
          <span className="text-xs font-medium text-foreground">Filter</span>
          <Input
            value={q}
            onChange={(e) => {
              setOffset(0);
              setQ(e.target.value);
            }}
            placeholder="Query, user, or person"
          />
        </label>
        <label className="block w-full space-y-1 sm:w-44">
          <span className="text-xs font-medium text-foreground">Status</span>
          <select
            value={status}
            onChange={(e) => {
              setOffset(0);
              setStatus(e.target.value);
            }}
            className="h-10 w-full rounded-lg border border-input bg-background px-3 text-sm outline-none focus:border-ring focus:ring-2 focus:ring-ring/20"
          >
            <option value="">All</option>
            <option value="ok">ok</option>
            <option value="error">error</option>
            <option value="cancelled">cancelled</option>
            <option value="validation_error">validation_error</option>
          </select>
        </label>
        <Button variant="secondary" onClick={() => void load()} disabled={loading}>
          Refresh
        </Button>
      </Card>

      {error && (
        <div className="rounded-lg border border-destructive/30 bg-destructive/10 px-4 py-3 text-sm text-destructive">
          {error}
        </div>
      )}

      <Card className="overflow-x-auto p-0">
        <table className="min-w-full text-left text-sm">
          <thead className="border-b border-border bg-muted/40 text-xs uppercase tracking-wide text-muted-foreground">
            <tr>
              <th className="px-3 py-2 font-medium">Query</th>
              <th className="px-3 py-2 font-medium">User</th>
              <th className="px-3 py-2 font-medium">When</th>
              <th className="px-3 py-2 font-medium">TAT</th>
              <th className="px-3 py-2 font-medium">Status</th>
              <th className="px-3 py-2 font-medium">Files</th>
              <th className="px-3 py-2 font-medium">Moments</th>
              <th className="px-3 py-2 font-medium" />
            </tr>
          </thead>
          <tbody>
            {loading && items.length === 0 ? (
              <tr>
                <td colSpan={8} className="px-3 py-8 text-center text-muted-foreground">
                  <LoadingLabel>Loading audits…</LoadingLabel>
                </td>
              </tr>
            ) : items.length === 0 ? (
              <tr>
                <td colSpan={8} className="px-3 py-8 text-center text-muted-foreground">
                  No search audits yet.
                </td>
              </tr>
            ) : (
              items.map((row) => (
                <tr key={row.id} className="border-b border-border/70 last:border-0">
                  <td className="max-w-[14rem] truncate px-3 py-2 font-medium text-foreground">
                    {row.query_text || "—"}
                  </td>
                  <td className="max-w-[12rem] truncate px-3 py-2 text-muted-foreground">
                    {row.user_email || "—"}
                  </td>
                  <td className="whitespace-nowrap px-3 py-2 text-muted-foreground">
                    {formatWhen(row.created_at)}
                  </td>
                  <td className="whitespace-nowrap px-3 py-2">{formatTat(row.tat_ms)}</td>
                  <td className="whitespace-nowrap px-3 py-2">
                    <span className="rounded-md bg-muted px-2 py-0.5 text-xs font-medium">
                      {row.status}
                    </span>
                  </td>
                  <td className="px-3 py-2">{row.file_count}</td>
                  <td className="px-3 py-2">{row.moment_count}</td>
                  <td className="px-3 py-2 text-right">
                    <Link
                      href={`/admin/search-audit/${row.id}`}
                      className="text-sm font-medium text-primary hover:underline"
                    >
                      Detail
                    </Link>
                  </td>
                </tr>
              ))
            )}
          </tbody>
        </table>
      </Card>

      <div className="flex items-center justify-between gap-3 text-sm text-muted-foreground">
        <span>
          {total} total · page {page} / {pageCount}
        </span>
        <div className="flex gap-2">
          <Button
            variant="secondary"
            disabled={offset <= 0 || loading}
            onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))}
          >
            Previous
          </Button>
          <Button
            variant="secondary"
            disabled={offset + PAGE_SIZE >= total || loading}
            onClick={() => setOffset(offset + PAGE_SIZE)}
          >
            Next
          </Button>
        </div>
      </div>
    </div>
  );
}
