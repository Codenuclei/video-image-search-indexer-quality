"""Detect English vs non-English transcript text for carousel hook preference."""

from __future__ import annotations

import re
from typing import Iterable

# Devanagari (Hindi/Marathi/etc.) and common Indic blocks seen in Hinglish videos.
_INDIC_RE = re.compile(
    r"[\u0900-\u097F\u0980-\u09FF\u0A00-\u0A7F\u0A80-\u0AFF"
    r"\u0B00-\u0B7F\u0B80-\u0BFF\u0C00-\u0C7F\u0C80-\u0CFF"
    r"\u0D00-\u0D7F]"
)
_CJK_RE = re.compile(r"[\u3040-\u30FF\u3400-\u9FFF\uAC00-\uD7AF]")
_LATIN_LETTER_RE = re.compile(r"[A-Za-z]")
_LETTER_RE = re.compile(r"[^\W\d_]", re.UNICODE)

# Common Hinglish / romanized Hindi tokens (lowercase). Used when script is Latin
# but the line is still not natural English. Avoid tokens that are also English words.
_HINGLISH_TOKENS = {
    "hai", "hain", "nahi", "nahin", "kyunki", "kyuki", "kya", "kyu", "kyun",
    "toh", "tha", "thi", "aur", "yeh", "woh", "hum", "aap",
    "tum", "bhai", "yaar", "accha", "achha", "bahut", "bohot", "matlab",
    "phir", "abhi", "waise", "bilkul", "sach", "sahi", "galat", "dekho",
    "sunao", "bolo", "karo", "karte", "karna", "hua", "hui", "gaya", "gayi",
    "raha", "rahi", "rahe", "liye", "iska", "uska", "hamara",
    "apka", "aapki", "zyada", "thoda", "pehle", "baad", "kyaa",
    "nahiin", "kuch", "sabse", "wahan", "yahan", "kaise", "kaisa",
}


def has_indic_script(text: str) -> bool:
    return bool(_INDIC_RE.search(text or ""))


def has_cjk_script(text: str) -> bool:
    return bool(_CJK_RE.search(text or ""))


def latin_letter_ratio(text: str) -> float:
    letters = _LETTER_RE.findall(text or "")
    if not letters:
        return 1.0  # no letters → treat as neutral / English-safe
    latin = sum(1 for ch in letters if _LATIN_LETTER_RE.fullmatch(ch))
    return latin / len(letters)


def hinglish_token_hits(text: str) -> int:
    toks = re.findall(r"[a-zA-Z']+", (text or "").lower())
    if not toks:
        return 0
    return sum(1 for t in toks if t in _HINGLISH_TOKENS)


def is_english_text(text: str, *, min_latin_ratio: float = 0.85) -> bool:
    """
    True when text is suitable as an English carousel hook/topic.
    Rejects Devanagari/CJK-heavy lines and obvious romanized Hinglish.
    """
    cleaned = " ".join((text or "").split()).strip()
    if not cleaned:
        return True
    if has_indic_script(cleaned) or has_cjk_script(cleaned):
        return False
    if latin_letter_ratio(cleaned) < min_latin_ratio:
        return False
    toks = re.findall(r"[a-zA-Z']+", cleaned.lower())
    if len(toks) >= 5 and hinglish_token_hits(cleaned) >= max(2, len(toks) // 4):
        return False
    return True


def needs_english(text: str) -> bool:
    return not is_english_text(text)


def cues_need_english(cues: Iterable[tuple[float, float | None, str] | str]) -> bool:
    """True if a meaningful share of cue text is non-English."""
    samples: list[str] = []
    for item in cues:
        if isinstance(item, str):
            text = item
        else:
            text = item[2] if len(item) > 2 else ""
        text = " ".join((text or "").split()).strip()
        if text:
            samples.append(text)
        if len(samples) >= 24:
            break
    if not samples:
        return False
    non_en = sum(1 for s in samples if needs_english(s))
    return non_en >= max(1, (len(samples) + 1) // 2)


def prefer_english_cues(
    cues: list[tuple[float, float | None, str]],
) -> list[tuple[float, float | None, str]]:
    """
    If the cue list mixes English and non-English lines, keep English ones.
    If everything is non-English, return the original list unchanged.
    """
    if not cues:
        return cues
    english = [(s, e, t) for s, e, t in cues if is_english_text(t or "")]
    if len(english) >= max(2, len(cues) // 3):
        return english
    return cues


def english_text_for_window(
    english_cues: list[tuple[float, float | None, str]],
    *,
    start_sec: float,
    end_sec: float | None,
    pad_sec: float = 1.5,
) -> str | None:
    """Stitch English cue text overlapping a time window (hook alignment)."""
    lo = float(start_sec) - pad_sec
    hi = float(end_sec) + pad_sec if end_sec is not None else float(start_sec) + 12.0
    parts: list[str] = []
    for s, e, t in english_cues:
        text = " ".join((t or "").split()).strip()
        if not text or not is_english_text(text):
            continue
        cue_end = float(e) if e is not None else float(s) + 2.0
        if cue_end < lo or float(s) > hi:
            continue
        parts.append(text)
        if len(" ".join(parts).split()) >= 36:
            break
    if not parts:
        return None
    joined = re.sub(r"\s+", " ", " ".join(parts)).strip()
    return joined or None
