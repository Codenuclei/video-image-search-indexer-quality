/** Open a backend download URL. Avoids fetch/CORS (cross-origin blob downloads fail in the browser). */
export async function downloadFromUrl(url: string, filename: string): Promise<void> {
  const safeName =
    (filename || "download").replace(/[<>:"/\\|?*\u0000-\u001f]/g, "_").trim() || "download";

  let href = url;
  try {
    const parsed = new URL(url, typeof window !== "undefined" ? window.location.href : undefined);
    // Frame endpoints need attachment disposition or the browser just displays the JPEG.
    if (parsed.pathname.includes("/frame") && parsed.searchParams.get("download") !== "1") {
      parsed.searchParams.set("download", "1");
      parsed.searchParams.set("filename", safeName);
    }
    href = parsed.toString();
  } catch {
    /* keep raw url */
  }

  const a = document.createElement("a");
  a.href = href;
  a.target = "_blank";
  a.rel = "noopener noreferrer";
  a.download = safeName;
  document.body.appendChild(a);
  a.click();
  a.remove();
}
