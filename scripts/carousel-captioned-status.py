#!/usr/bin/env python3
"""Report carousel readiness for captioned videos only.

Captioned means the same thing here as in the carousel pickers and the
background worker: at least one non-empty transcript cue.
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
os.environ.setdefault("SKIP_STARTUP_INDEX", "1")

from sqlalchemy import and_, func, select  # noqa: E402

from app.db.models import (  # noqa: E402
    CarouselGenerationSave,
    DriveFile,
    DriveFileStatus,
    Media,
    VideoSegment,
)
from app.db.session import get_session_factory  # noqa: E402


async def main() -> int:
    cue_count = (
        select(func.count(VideoSegment.id))
        .select_from(Media)
        .join(
            VideoSegment,
            and_(VideoSegment.media_id == Media.id, VideoSegment.text != ""),
        )
        .where(Media.drive_file_id == DriveFile.id)
        .scalar_subquery()
    )
    session_factory = get_session_factory()
    async with session_factory() as session:
        ready = {
            str(x)
            for x in (
                await session.execute(
                    select(CarouselGenerationSave.drive_file_id).where(
                        CarouselGenerationSave.kind == "carousel",
                        CarouselGenerationSave.status == "ready",
                    )
                )
            ).scalars().all()
        }
        rows = list(
            (
                await session.execute(
                    select(
                        DriveFile.id,
                        DriveFile.name,
                        DriveFile.carousel_status,
                        DriveFile.carousel_attempts,
                        DriveFile.carousel_error,
                        cue_count.label("cues"),
                    )
                    .where(
                        DriveFile.status == DriveFileStatus.PROCESSED,
                        DriveFile.mime_type.like("video/%"),
                        cue_count >= 2,
                    )
                    .order_by(cue_count.desc())
                )
            ).all()
        )

    done = 0
    for file_id, name, status, attempts, error, cues in rows:
        has_artifact = str(file_id) in ready
        done += 1 if has_artifact else 0
        flag = "ready " if has_artifact else "      "
        note = f" err={(error or '')[:70]}" if status == "error" else ""
        print(
            f"{flag} status={status or 'idle':10s} attempts={attempts or 0} "
            f"cues={int(cues or 0):5d} {(name or '')[:64]}{note}"
        )
    print(f"\ncaptioned={len(rows)} ready={done} pending={len(rows) - done}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
