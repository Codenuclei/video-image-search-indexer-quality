"use client";

import { useCallback, useEffect, useState } from "react";
import { apiClient, type Settings } from "@/lib/api";
import { Button, Card, Input } from "@/components/ui";

export default function SettingsPage() {
  const [settings, setSettings] = useState<Settings | null>(null);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);

  useEffect(() => {
    apiClient
      .settings()
      .then(setSettings)
      .catch((e) => setError(e instanceof Error ? e.message : "Failed to load settings"));
  }, []);

  const persist = useCallback(async (patch: Partial<Settings>) => {
    setSaving(true);
    setError(null);
    try {
      const updated = await apiClient.updateSettings(patch);
      setSettings(updated);
      setSaved(true);
      setTimeout(() => setSaved(false), 1500);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to save");
    } finally {
      setSaving(false);
    }
  }, []);

  async function saveInterval() {
    if (!settings) return;
    await persist({ auto_index_interval_seconds: settings.auto_index_interval_seconds });
  }

  if (!settings) {
    return <p className="text-zinc-400">{error ?? "Loading..."}</p>;
  }

  return (
    <div className="max-w-lg space-y-6">
      <div className="flex items-center justify-between gap-3">
        <div>
          <h2 className="text-2xl font-semibold">Settings</h2>
          <p className="text-sm text-zinc-400">Saved to database — persists across restarts</p>
        </div>
        {saving && <span className="text-xs text-zinc-500">Saving…</span>}
        {!saving && saved && <span className="text-xs text-green-400">Saved</span>}
      </div>

      {error && <Card className="border-destructive text-destructive">{error}</Card>}

      <Card className="space-y-2 text-sm text-zinc-400">
        <p>
          Model: <span className="text-zinc-200">{settings.gemini_model}</span>
        </p>
        <p>
          File Search store:{" "}
          <span className="text-zinc-200">{settings.gemini_file_search_store_display_name}</span>
        </p>
        <p className="text-xs">Set GEMINI_API_KEY and store name in backend <code>.env</code>.</p>
      </Card>

      <Card className="space-y-4">
        <h3 className="font-medium">Search</h3>
        <label className="flex cursor-pointer items-start gap-3 text-sm">
          <input
            type="checkbox"
            checked={settings.gemini_file_search_search_enabled}
            disabled={saving}
            onChange={(e) => {
              const v = e.target.checked;
              setSettings({ ...settings, gemini_file_search_search_enabled: v });
              persist({ gemini_file_search_search_enabled: v });
            }}
            className="mt-0.5 h-4 w-4 shrink-0 rounded border-border bg-background accent-blue-500"
          />
          <span>
            <span className="text-zinc-200">Gemini File Search at query time</span>
            <span className="mt-1 block text-xs text-zinc-500">
              Off by default — Qdrant vectors only. Turn on for an extra semantic pass (slower).
            </span>
          </span>
        </label>
        <label className="flex cursor-pointer items-start gap-3 text-sm">
          <input
            type="checkbox"
            checked={settings.search_parallel_variants_enabled}
            disabled={saving}
            onChange={(e) => {
              const v = e.target.checked;
              setSettings({ ...settings, search_parallel_variants_enabled: v });
              persist({ search_parallel_variants_enabled: v });
            }}
            className="mt-0.5 h-4 w-4 shrink-0 rounded border-border bg-background accent-blue-500"
          />
          <span>
            <span className="text-zinc-200">Parallel query variants</span>
            <span className="mt-1 block text-xs text-zinc-500">
              Off by default — sequential embed+Qdrant (more reliable). On = faster but can hurt quality.
            </span>
          </span>
        </label>
        <label className="flex cursor-pointer items-start gap-3 text-sm">
          <input
            type="checkbox"
            checked={settings.search_use_captions}
            disabled={saving}
            onChange={(e) => {
              const v = e.target.checked;
              setSettings({ ...settings, search_use_captions: v });
              persist({ search_use_captions: v });
            }}
            className="mt-0.5 h-4 w-4 shrink-0 rounded border-border bg-background accent-blue-500"
          />
          <span>
            <span className="text-zinc-200">Use captions in search</span>
            <span className="mt-1 block text-xs text-zinc-500">
              Default for the Search page Captions toggle. Synced across devices.
            </span>
          </span>
        </label>
        <label className="flex cursor-pointer items-start gap-3 text-sm">
          <input
            type="checkbox"
            checked={settings.search_rerank_enabled}
            disabled={saving}
            onChange={(e) => {
              const v = e.target.checked;
              setSettings({ ...settings, search_rerank_enabled: v });
              persist({ search_rerank_enabled: v });
            }}
            className="mt-0.5 h-4 w-4 shrink-0 rounded border-border bg-background accent-blue-500"
          />
          <span>
            <span className="text-zinc-200">Video re-rank default</span>
            <span className="mt-1 block text-xs text-zinc-500">
              Default for the Search page Re-rank toggle. Synced across devices.
            </span>
          </span>
        </label>
      </Card>

      <Card className="space-y-4">
        <h3 className="font-medium">Auto indexing</h3>
        <label className="flex cursor-pointer items-center gap-3 text-sm">
          <input
            type="checkbox"
            checked={settings.auto_index_enabled}
            disabled={saving}
            onChange={(e) => {
              const v = e.target.checked;
              setSettings({ ...settings, auto_index_enabled: v });
              persist({ auto_index_enabled: v });
            }}
            className="h-4 w-4 rounded border-border bg-background accent-blue-500"
          />
          Automatically sync Drive and upload new or changed files to Gemini
        </label>
        <label className="flex cursor-pointer items-start gap-3 text-sm">
          <input
            type="checkbox"
            checked={settings.follow_shortcut_folders}
            disabled={saving}
            onChange={(e) => {
              const v = e.target.checked;
              setSettings({ ...settings, follow_shortcut_folders: v });
              persist({ follow_shortcut_folders: v });
            }}
            className="mt-0.5 h-4 w-4 shrink-0 rounded border-border bg-background accent-blue-500"
          />
          <span>
            <span className="text-zinc-200">Follow folder shortcuts when syncing</span>
            <span className="mt-1 block text-xs text-zinc-500">
              Pull files from shortcut folders (e.g. &quot;Master Folder&quot;, &quot;UG iPhone Data&quot;) inside the
              connected directory.
            </span>
          </span>
        </label>
        <p className="text-xs text-zinc-500">
          Drive Connector polls every 30s and sends a webhook when files change. This interval is a
          fallback poll.
        </p>
        <div>
          <label className="text-sm text-zinc-400">Fallback poll interval (seconds)</label>
          <Input
            type="number"
            step="30"
            min="30"
            value={settings.auto_index_interval_seconds}
            onChange={(e) =>
              setSettings({ ...settings, auto_index_interval_seconds: Number(e.target.value) })
            }
          />
        </div>
        <Button onClick={saveInterval} disabled={saving}>
          Save interval
        </Button>
      </Card>
    </div>
  );
}
