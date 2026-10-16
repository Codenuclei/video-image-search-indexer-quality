import { toast } from "sonner";

let lastKey = "";
let lastAt = 0;

function looksLikeAbortMessage(message: string): boolean {
  const lower = message.toLowerCase();
  return (
    lower === "aborterror" ||
    lower.startsWith("aborterror:") ||
    lower.includes("signal is aborted") ||
    lower.includes("aborted without reason") ||
    lower.includes("the operation was aborted") ||
    lower.includes("the user aborted a request") ||
    lower.includes("request aborted") ||
    lower.includes("fetch aborted") ||
    lower === "aborted" ||
    lower === "search cancelled."
  );
}

/** Corner Sonner toast for every user-facing API failure. Dedupes rapid repeats. */
export function toastApiError(message: string): void {
  const msg = (message || "").trim();
  if (!msg) return;
  // Never surface raw AbortController / fetch-cancel noise (incl. DOMException text).
  if (looksLikeAbortMessage(msg)) return;
  const now = Date.now();
  if (msg === lastKey && now - lastAt < 1500) return;
  lastKey = msg;
  lastAt = now;
  toast.error(msg);
}
