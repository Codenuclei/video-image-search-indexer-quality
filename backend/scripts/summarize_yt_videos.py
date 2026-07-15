import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.video.youtube_transcript import fetch_youtube_captions

VIDEOS = [
    ("j2lcnmLGSxQ", "This AI Caught a 5 Lakh Theft Nobody Was Watching"),
    ("ICi-rgwvj_o", "100 Cr Footwear Without a Single Factory"),
    ("PAGwQi1Dy3Y", "He made 2,34,55,432 selling game clothes - age 19"),
    ("_FBivfgOvuE", "How to Get Your First 10 Customers"),
    ("rWUWfj_PqmM", "The New Way To Build A Startup"),
]


def summarize_transcript(cues, max_chars: int = 4000) -> str:
    text = " ".join(c.text for c in cues)
    return text[:max_chars]


def key_phrases(cues, n: int = 12) -> list[str]:
    """Sample evenly spaced lines for topic fingerprint."""
    if not cues:
        return []
    step = max(1, len(cues) // n)
    return [cues[i].text for i in range(0, len(cues), step)][:n]


async def main() -> None:
    for vid, title in VIDEOS:
        cues = await fetch_youtube_captions(vid)
        duration = cues[-1].end_sec if cues else 0
        print("=" * 70)
        print(f"ID: {vid}")
        print(f"TITLE: {title}")
        print(f"SOURCE: transcript (InnerTube captions)")
        print(f"CUES: {len(cues)} | ~{duration/60:.1f} min")
        print("SAMPLE LINES:")
        for line in key_phrases(cues, 15):
            safe = line.encode("ascii", "replace").decode()
            print(f"  - {safe}")
        print("FULL TEXT PREVIEW:")
        preview = summarize_transcript(cues, 2500)
        print(preview.encode("ascii", "replace").decode())
        print()


if __name__ == "__main__":
    asyncio.run(main())
