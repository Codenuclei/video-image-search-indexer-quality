import { clsx, type ClassValue } from "clsx";

export function cn(...inputs: ClassValue[]) {
  return clsx(inputs);
}

export function formatDate(iso: string) {
  return new Date(iso).toLocaleString();
}

// Character-class helpers use case mapping instead of the /u regex flag, which the
// project's ES5 TypeScript target rejects. They are Unicode-aware (Á, é, ñ, ...).
function isUpperChar(ch: string): boolean {
  return ch !== ch.toLowerCase() && ch === ch.toUpperCase();
}
function isLowerChar(ch: string): boolean {
  return ch !== ch.toUpperCase() && ch === ch.toLowerCase();
}
function isWordChar(ch: string): boolean {
  return ch.toLowerCase() !== ch.toUpperCase() || (ch >= "0" && ch <= "9");
}

/** Title-case display for roster roles / status labels without mutating source data. */
export function toTitleCase(value: string): string {
  const text = (value || "").trim();
  if (!text) return text;
  // Split on each separator token (do not use a repeated capturing group — JS
  // only keeps the last capture, which drops commas like "Director, Masters'").
  return text
    .split(/(\s+|[-/|,])/)
    .map((part) => {
      if (!part || /^\s+$/.test(part) || /^[-/|,]$/.test(part)) return part;
      const chars = Array.from(part);
      // Keep acronyms (all upper-case letters/digits, 2+ chars).
      if (chars.length >= 2 && chars.every((c) => isUpperChar(c) || (c >= "0" && c <= "9"))) {
        return part;
      }
      // Preserve CamelCase / internal capitals (McDonald, PhD, O'Brien, eBay).
      for (let i = 1; i < chars.length; i++) {
        if (isUpperChar(chars[i]) && (isLowerChar(chars[i - 1]) || chars[i - 1] === "'")) {
          return part;
        }
      }
      if (chars.filter(isUpperChar).length >= 2) return part;
      // Skip leading punctuation like "(" so "(acting)" -> "(Acting)", and accented
      // first letters (Álvaro) are treated as letters, not punctuation.
      const start = chars.findIndex(isWordChar);
      if (start === -1) return part;
      return (
        chars.slice(0, start).join("") +
        chars[start].toUpperCase() +
        chars.slice(start + 1).join("").toLowerCase()
      );
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
