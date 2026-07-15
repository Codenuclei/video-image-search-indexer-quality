import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.video.youtube_transcript import fetch_youtube_captions, youtube_id_from_filename

VIDEOS = [
    "j2lcnmLGSxQ",
    "ICi-rgwvj_o",
    "PAGwQi1Dy3Y",
    "_FBivfgOvuE",
    "rWUWfj_PqmM",
]


async def main() -> None:
    for vid in VIDEOS:
        cues = await fetch_youtube_captions(vid)
        sample = cues[0].text[:80].encode("ascii", "replace").decode() if cues else "(none)"
        print(f"{vid}: {len(cues)} cues - {sample}")


if __name__ == "__main__":
    asyncio.run(main())
