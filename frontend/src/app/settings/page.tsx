"use client";

import { useEffect, useState } from "react";
import { apiClient, type Settings } from "@/lib/api";
import { Button, Card, Input } from "@/components/ui";

export default function SettingsPage() {
  const [settings, setSettings] = useState<Settings | null>(null);
  const [saved, setSaved] = useState(false);

  useEffect(() => {
    apiClient.settings().then(setSettings);
  }, []);

  async function save() {
    if (!settings) return;
    const updated = await apiClient.updateSettings({
      auto_index_enabled: settings.auto_index_enabled,
      auto_index_interval_seconds: settings.auto_index_interval_seconds,
    });
    setSettings(updated);
    setSaved(true);
    setTimeout(() => setSaved(false), 2000);
  }

  if (!settings) return <p className="text-zinc-400">Loading...</p>;

  return (
    <div className="max-w-lg space-y-6">
      <div>
        <h2 className="text-2xl font-semibold">Settings</h2>
        <p className="text-sm text-zinc-400">Gemini File Search and auto-indexing</p>
      </div>

      <Card className="space-y-2 text-sm text-zinc-400">
        <p>Model: <span className="text-zinc-200">{settings.gemini_model}</span></p>
        <p>File Search store: <span className="text-zinc-200">{settings.gemini_file_search_store_display_name}</span></p>
        <p className="text-xs">Set GEMINI_API_KEY and store name in backend <code>.env</code>.</p>
      </Card>

      <Card className="space-y-4">
        <h3 className="font-medium">Auto indexing</h3>
        <label className="flex cursor-pointer items-center gap-3 text-sm">
          <input
            type="checkbox"
            checked={settings.auto_index_enabled}
            onChange={(e) => setSettings({ ...settings, auto_index_enabled: e.target.checked })}
            className="h-4 w-4 rounded border-border bg-background accent-blue-500"
          />
          Automatically sync Drive and upload new or changed files to Gemini
        </label>
        <p className="text-xs text-zinc-500">
          Drive Connector polls every 30s and sends a webhook when files change. This interval is a fallback poll.
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
        <div className="flex items-center gap-3">
          <Button onClick={save}>Save</Button>
          {saved && <span className="text-sm text-green-400">Saved</span>}
        </div>
      </Card>
    </div>
  );
}
