import { toast } from "sonner";

let lastKey = "";
let lastAt = 0;

/** Corner Sonner toast for every user-facing API failure. Dedupes rapid repeats. */
export function toastApiError(message: string): void {
  const msg = (message || "").trim();
  if (!msg) return;
  const now = Date.now();
  if (msg === lastKey && now - lastAt < 1500) return;
  lastKey = msg;
  lastAt = now;
  toast.error(msg);
}
