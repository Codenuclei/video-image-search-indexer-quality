"use client";

import { useCallback, useEffect, useState } from "react";
import Script from "next/script";
import { FolderOpen } from "lucide-react";
import { API_BASE, apiClient, type DriveSession } from "@/lib/api";
import { Button, LoadingLabel } from "@/components/ui";
import { toastApiError } from "@/lib/toast-api-error";

declare global {
  interface Window {
    gapi: any;
    google: any;
    _pickerApiLoaded?: boolean;
  }
}

function driveConnectHref(): string {
  if (typeof window === "undefined") return `${API_BASE}/auth/google`;
  const next = `${window.location.pathname}${window.location.search}`;
  return `${API_BASE}/auth/google?return_to=${encodeURIComponent(next)}`;
}

export function DriveSessionBar({ compact = false }: { compact?: boolean }) {
  const [driveSession, setDriveSession] = useState<DriveSession | null>(null);
  const [pickerBusy, setPickerBusy] = useState(false);

  const refresh = useCallback(async () => {
    try {
      const session = await apiClient.driveSession();
      setDriveSession(session);
    } catch {
      setDriveSession(null);
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  async function openPicker() {
    setPickerBusy(true);
    try {
      const { accessToken, apiKey, appId } = await apiClient.driveToken();
      if (!apiKey) {
        toastApiError(
          "GOOGLE_API_KEY is missing on the backend. Set a Browser API key (Drive + Picker APIs) in backend/.env and restart."
        );
        return;
      }
      const FOLDER_MIME = "application/vnd.google-apps.folder";

      if (!window._pickerApiLoaded) {
        await new Promise<void>((resolve) => window.gapi.load("picker", resolve));
        window._pickerApiLoaded = true;
      }

      const myDriveFolderView = new window.google.picker.DocsView(window.google.picker.ViewId.FOLDERS)
        .setEnableDrives(false)
        .setIncludeFolders(true)
        .setSelectFolderEnabled(true)
        .setMimeTypes(FOLDER_MIME)
        .setLabel("My Drive folders");

      const myDriveMediaView = new window.google.picker.DocsView(window.google.picker.ViewId.DOCS_IMAGES_AND_VIDEOS)
        .setEnableDrives(false)
        .setIncludeFolders(true)
        .setSelectFolderEnabled(true)
        .setLabel("My Drive images & videos");

      const sharedDriveView = new window.google.picker.DocsView(window.google.picker.ViewId.DOCS)
        .setEnableDrives(true)
        .setIncludeFolders(true)
        .setSelectFolderEnabled(true)
        .setLabel("Shared drives");

      const builder = new window.google.picker.PickerBuilder()
        .setTitle("Choose a folder to index")
        .addView(myDriveFolderView)
        .addView(myDriveMediaView)
        .addView(sharedDriveView)
        .setOAuthToken(accessToken)
        .setDeveloperKey(apiKey)
        .enableFeature(window.google.picker.Feature.SUPPORT_DRIVES)
        .setCallback(async (data: any) => {
          if (data.action !== window.google.picker.Action.PICKED) return;
          const doc = data.docs[0];
          if (doc.mimeType && doc.mimeType !== FOLDER_MIME) {
            toastApiError(
              `"${doc.name}" is a file. Use Select folder (top-right) to choose the folder to index.`
            );
            return;
          }
          await apiClient.saveDriveFolder(doc.id, doc.name);
          await apiClient.syncDriveFiles().catch(() => {});
          await refresh();
        });

      if (appId) builder.setAppId(appId);
      builder.build().setVisible(true);
    } catch {
      /* driveToken already toasted */
    } finally {
      setPickerBusy(false);
    }
  }

  if (compact) {
    const driveEmail = driveSession?.email?.trim() || null;
    const folderName = driveSession?.selected_folder?.name?.trim() || null;
    return (
      <>
        <Script src="https://apis.google.com/js/api.js" strategy="lazyOnload" />
        {driveSession?.connected ? (
          <Button
            variant="secondary"
            onClick={() => void openPicker()}
            disabled={pickerBusy}
            className="inline-flex h-auto max-w-[min(18rem,40vw)] items-center gap-2 px-3 py-1.5"
            title={
              [folderName ?? "Choose folder", driveEmail, "Click to change folder"]
                .filter(Boolean)
                .join(" · ")
            }
          >
            <FolderOpen size={16} className="shrink-0" />
            {pickerBusy ? (
              <LoadingLabel>Opening…</LoadingLabel>
            ) : (
              <span className="min-w-0 text-left">
                <span className="block truncate text-xs font-medium leading-tight">
                  {folderName ?? "Choose Folder"}
                </span>
                {driveEmail && (
                  <span className="block truncate text-[10px] font-normal leading-tight text-muted-foreground">
                    {driveEmail}
                  </span>
                )}
              </span>
            )}
          </Button>
        ) : (
          <Button onClick={() => (window.location.href = driveConnectHref())} className="inline-flex items-center gap-2">
            Connect Google Drive
          </Button>
        )}
      </>
    );
  }

  return (
    <>
      <Script src="https://apis.google.com/js/api.js" strategy="lazyOnload" />
      <div className="flex flex-wrap items-center justify-between gap-3 rounded-2xl border border-border bg-card px-4 py-3 shadow-sm">
        <div className="min-w-0">
          <p className="text-sm font-medium">
            {driveSession?.connected ? "Google Drive connected" : "Google Drive not connected"}
          </p>
          <p className="truncate text-xs text-muted-foreground">
            {driveSession?.connected
              ? `${driveSession.email ?? ""}${
                  driveSession.selected_folder ? ` · ${driveSession.selected_folder.name}` : " · No folder selected"
                }`
              : "Connect a Drive account, then choose a folder to index."}
          </p>
        </div>
        <div className="flex flex-wrap gap-2">
          {driveSession?.connected ? (
            <>
              <Button onClick={() => void openPicker()} disabled={pickerBusy}>
                {pickerBusy ? (
                  <LoadingLabel>Opening…</LoadingLabel>
                ) : driveSession.selected_folder ? (
                  "Change Folder"
                ) : (
                  "Choose Folder"
                )}
              </Button>
              <Button
                variant="secondary"
                onClick={async () => {
                  await apiClient.driveLogout();
                  await refresh();
                }}
              >
                Disconnect
              </Button>
            </>
          ) : (
            <Button onClick={() => (window.location.href = driveConnectHref())}>
              Connect Google Drive
            </Button>
          )}
        </div>
      </div>
    </>
  );
}
