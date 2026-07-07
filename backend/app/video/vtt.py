from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class VttCue:
    start_sec: float
    end_sec: float
    text: str


_TIME_RE = re.compile(
    r"(?P<h1>\d{2}):(?P<m1>\d{2}):(?P<s1>\d{2})\.(?P<ms1>\d{3})\s*-->\s*"
    r"(?P<h2>\d{2}):(?P<m2>\d{2}):(?P<s2>\d{2})\.(?P<ms2>\d{3})"
)


def _parse_timestamp(h: str, m: str, s: str, ms: str) -> float:
    return int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000.0


def parse_vtt(content: str) -> list[VttCue]:
    """Parse WebVTT cues (timestamps + text)."""
    cues: list[VttCue] = []
    lines = content.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        match = _TIME_RE.match(line)
        if match:
            start = _parse_timestamp(
                match.group("h1"), match.group("m1"), match.group("s1"), match.group("ms1")
            )
            end = _parse_timestamp(
                match.group("h2"), match.group("m2"), match.group("s2"), match.group("ms2")
            )
            i += 1
            text_lines: list[str] = []
            while i < len(lines) and lines[i].strip():
                text_lines.append(_strip_tags(lines[i].strip()))
                i += 1
            text = " ".join(text_lines).strip()
            if text:
                cues.append(VttCue(start_sec=start, end_sec=end, text=text))
            continue
        i += 1
    return cues


def _strip_tags(text: str) -> str:
    return re.sub(r"<[^>]+>", "", text).strip()
