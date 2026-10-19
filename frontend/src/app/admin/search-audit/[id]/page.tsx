"use client";

import Link from "next/link";
import { useEffect, useState, type ReactNode } from "react";
import { useParams, useRouter } from "next/navigation";
import { useAuthSession } from "@/components/auth-gate";
import { Button, Card, LoadingLabel } from "@/components/ui";
import {
  formatApiError,
  getSearchAudit,
  type SearchAuditDetail,
  type SearchMoment,
  type SearchResultFile,
} from "@/lib/api";

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

function MetaRow({ label, value }: { label: string; value: ReactNode }) {
  return (
    <div className="grid grid-cols-[8rem_1fr] gap-2 text-sm sm:grid-cols-[10rem_1fr]">
      <dt className="text-muted-foreground">{label}</dt>
      <dd className="break-words text-foreground">{value ?? "—"}</dd>
    </div>
  );
}

export default function SearchAuditDetailPage() {
  const router = useRouter();
  const params = useParams<{ id: string }>();
  const { isAdmin: allowed } = useAuthSession();
  const [detail, setDetail] = useState<SearchAuditDetail | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!allowed) router.replace("/");
  }, [allowed, router]);

  useEffect(() => {
    if (!allowed) return;
    const id = Number(params.id);
    if (!Number.isFinite(id) || id <= 0) {
      setError("Invalid audit id");
      setLoading(false);
      return;
    }
    let cancelled = false;
    setLoading(true);
    setError(null);
    void getSearchAudit(id)
      .then((data) => {
        if (!cancelled) setDetail(data);
      })
      .catch((err) => {
        if (!cancelled) {
          setError(formatApiError(err, "Could not load audit detail"));
          setDetail(null);
        }
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [allowed, params.id]);

  if (!allowed) return null;

  const snapshot = detail?.results_json;
  const files = (snapshot?.files ?? []) as SearchResultFile[];
  const moments = (snapshot?.moments ?? []) as SearchMoment[];
  const answer = typeof snapshot?.answer === "string" ? snapshot.answer : "";

  return (
    <div className="mx-auto max-w-5xl space-y-4 p-4 sm:p-6">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="text-2xl font-semibold text-foreground">Search audit #{params.id}</h1>
          <p className="mt-1 text-sm text-muted-foreground">
            Stored request inputs and result snapshot.
          </p>
        </div>
        <Button variant="secondary" onClick={() => router.push("/admin/search-audit")}>
          Back to list
        </Button>
      </div>

      {loading ? (
        <Card>
          <LoadingLabel>Loading detail…</LoadingLabel>
        </Card>
      ) : error ? (
        <Card className="border-destructive/30 bg-destructive/10 text-destructive">{error}</Card>
      ) : detail ? (
        <>
          <Card className="space-y-3">
            <h2 className="text-sm font-semibold uppercase tracking-wide text-muted-foreground">
              Request
            </h2>
            <dl className="space-y-2">
              <MetaRow label="Query" value={detail.query_text || "—"} />
              <MetaRow label="User" value={detail.user_email || "—"} />
              <MetaRow label="When" value={formatWhen(detail.created_at)} />
              <MetaRow label="TAT" value={formatTat(detail.tat_ms)} />
              <MetaRow label="Status" value={`${detail.status}${detail.http_status != null ? ` (${detail.http_status})` : ""}`} />
              <MetaRow label="Person" value={detail.person || "—"} />
              <MetaRow label="Mime" value={detail.mime || "—"} />
              <MetaRow label="Folder" value={detail.folder_path || "—"} />
              <MetaRow label="Source" value={detail.source || "—"} />
              <MetaRow label="Rerank" value={detail.rerank || "—"} />
              <MetaRow label="Captions" value={detail.captions || "—"} />
              <MetaRow label="Cache" value={detail.cache || "—"} />
              <MetaRow label="Policy" value={detail.retrieval_policy || "—"} />
              <MetaRow label="Search id" value={detail.search_id || "—"} />
              <MetaRow label="Client" value={detail.client_host || "—"} />
              {detail.error_detail ? (
                <MetaRow label="Error" value={detail.error_detail} />
              ) : null}
            </dl>
          </Card>

          <Card className="space-y-2">
            <h2 className="text-sm font-semibold uppercase tracking-wide text-muted-foreground">
              Answer
            </h2>
            <p className="whitespace-pre-wrap text-sm text-foreground">
              {answer.trim() ? answer : "—"}
            </p>
          </Card>

          <Card className="space-y-3">
            <h2 className="text-sm font-semibold uppercase tracking-wide text-muted-foreground">
              Files ({files.length})
            </h2>
            {files.length === 0 ? (
              <p className="text-sm text-muted-foreground">No files in snapshot.</p>
            ) : (
              <ul className="divide-y divide-border">
                {files.map((file) => (
                  <li key={file.drive_file_id} className="py-2 text-sm">
                    <div className="font-medium text-foreground">{file.name}</div>
                    <div className="text-muted-foreground">
                      {file.path || file.drive_file_id}
                      {file.score != null ? ` · score ${file.score.toFixed(3)}` : ""}
                    </div>
                  </li>
                ))}
              </ul>
            )}
          </Card>

          <Card className="space-y-3">
            <h2 className="text-sm font-semibold uppercase tracking-wide text-muted-foreground">
              Moments ({moments.length})
            </h2>
            {moments.length === 0 ? (
              <p className="text-sm text-muted-foreground">No moments in snapshot.</p>
            ) : (
              <ul className="divide-y divide-border">
                {moments.map((moment, index) => (
                  <li
                    key={`${moment.drive_file_id}-${moment.timestamp_sec}-${index}`}
                    className="py-2 text-sm"
                  >
                    <div className="font-medium text-foreground">
                      {moment.name || moment.drive_file_id} @ {moment.timestamp_sec}s
                    </div>
                    <div className="text-muted-foreground">
                      {moment.snippet || moment.match_type}
                    </div>
                  </li>
                ))}
              </ul>
            )}
          </Card>

          <p className="text-xs text-muted-foreground">
            <Link href="/admin/search-audit" className="text-primary hover:underline">
              ← All audits
            </Link>
          </p>
        </>
      ) : null}
    </div>
  );
}
