import { clsx, type ClassValue } from "clsx";

export function cn(...inputs: ClassValue[]) {
  return clsx(inputs);
}

export function formatDate(iso: string) {
  return new Date(iso).toLocaleString();
}

/** Title-case display for roster roles / status labels without mutating source data. */
export function toTitleCase(value: string): string {
  const text = (value || "").trim();
  if (!text) return text;
  return text
    .split(/(\s+|[-/|,])+/)
    .map((part) => {
      if (!part || /^\s+$/.test(part) || /^[-/|,]+$/.test(part)) return part;
      if (/^[A-Z0-9]{2,}$/.test(part)) return part; // keep acronyms
      return part.charAt(0).toUpperCase() + part.slice(1).toLowerCase();
    })
    .join("");
}

/** Format seconds as m:ss (or h:mm:ss for longer media). */
export function formatMediaClock(sec: number): string {
  if (!Number.isFinite(sec) || sec < 0) return "0:00";
  const total = Math.floor(sec);
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  if (h > 0) {
    return `${h}:${m.toString().padStart(2, "0")}:${s.toString().padStart(2, "0")}`;
  }
  return `${m}:${s.toString().padStart(2, "0")}`;
}
