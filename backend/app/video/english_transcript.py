"""Ensure stored video transcripts are complete English sentences with timestamps.

Pipeline for a selected Drive/YouTube video:
1. Load timed cue text (Postgres VideoSegment rows).
2. Stitch adjacent cue fragments into complete utterances (words unchanged).
3. If non-English → translate faithfully via configured LLM (Claude / OpenRouter / Gemini).
4. Validate: every segment translated, English, non-empty; timestamps preserved.
5. On any failure → raise (never write partial / invented results).
6. Replace non-English text segments + refresh Qdrant transcript RAG.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import DriveFile, Media, MediaType, VideoSegment
from app.search.english_text import cues_need_english, is_english_text

logger = logging.getLogger(__name__)

_BATCH_SIZE = 40
_SENTENCE_END_RE = re.compile(r"[.!?…][\"')\]]*$")


class EnglishTranscriptError(RuntimeError):
    """User-facing failure: do not invent / partial-write transcripts.

    ``str(exc)`` is safe to show in the UI as-is.
    """


# Friendly copy for common outcomes (also used by the API ``message`` field).
MSG_ALREADY_ENGLISH = "This transcript is already in English."
MSG_TRANSLATED = "Translated to English and saved."
MSG_STITCHED = "English transcript ready."
MSG_NO_TRANSCRIPT = (
    "We couldn’t find a transcript for this video yet. "
    "Generate captions first, then try again."
)
MSG_VIDEO_MISSING = "We couldn’t find that video in your library."
MSG_INCOMPLETE = (
    "The transcript looks incomplete, so we didn’t save a partial result. "
    "Try regenerating captions, then try again."
)
MSG_NOT_ENGLISH = (
    "We couldn’t get a reliable English transcript, so nothing was changed. "
    "Please try again."
)
MSG_TRANSLATE_FAILED = (
    "We couldn’t translate this transcript right now. "
    "Nothing was changed — please try again in a moment."
)
MSG_NO_LLM_KEY = (
    "Translation needs an AI key (Claude, OpenRouter, or Gemini). "
    "Add one in settings, then try again."
)
MSG_TIMESTAMP_BAD = (
    "The transcript timestamps look invalid, so we didn’t save anything. "
    "Please try again."
)
MSG_QDRANT_FAILED = (
    "The English transcript couldn’t be saved for search. "
    "Please try again in a moment."
)


@dataclass(frozen=True)
class TimedSentence:
    start_sec: float
    end_sec: float | None
    text: str


@dataclass
class EnglishTranscriptResult:
    drive_file_id: str
    media_id: int
    cue_count: int
    translated: bool
    already_english: bool
    language: str
    source: str
    llm_provider: str | None = None
    deleted_non_english: int = 0
    segments: list[TimedSentence] | None = None
    message: str = ""

    def user_message(self) -> str:
        if (self.message or "").strip():
            return self.message.strip()
        if self.translated:
            return f"{MSG_TRANSLATED} ({self.cue_count} sentences)."
        if self.already_english and self.source == "stored_en":
            return f"{MSG_ALREADY_ENGLISH} ({self.cue_count} sentences)."
        return f"{MSG_STITCHED} ({self.cue_count} sentences)."


def stitch_complete_sentences(
    cues: list[tuple[float, float | None, str]],
) -> list[TimedSentence]:
    """
    Merge adjacent cue fragments into complete spoken sentences.

    Does not invent or rewrite words — only joins cue text in order and
    assigns start_sec from the first cue and end_sec from the last cue in
    each utterance.
    """
    chunks: list[TimedSentence] = []
    buf_text: list[str] = []
    buf_start: float | None = None
    buf_end: float | None = None

    def flush(*, force: bool = False) -> None:
        nonlocal buf_text, buf_start, buf_end
        if not buf_text or buf_start is None:
            buf_text, buf_start, buf_end = [], None, None
            return
        text = re.sub(r"\s+", " ", " ".join(buf_text)).strip()
        words = text.split()
        if not text:
            buf_text, buf_start, buf_end = [], None, None
            return
        # Drop tiny scraps unless forced (e.g. end-of-video remainder).
        if not force and len(words) < 3:
            buf_text, buf_start, buf_end = [], None, None
            return
        chunks.append(
            TimedSentence(
                start_sec=float(buf_start),
                end_sec=float(buf_end) if buf_end is not None else None,
                text=text,
            )
        )
        buf_text, buf_start, buf_end = [], None, None

    for s, e, raw in cues:
        piece = " ".join((raw or "").split()).strip()
        if not piece:
            continue
        if buf_start is None:
            buf_start = float(s)
        buf_text.append(piece)
        buf_end = float(e) if e is not None else float(s)
        joined = " ".join(buf_text)
        words = len(joined.split())
        ends_thought = bool(_SENTENCE_END_RE.search(piece)) or words >= 22
        if ends_thought and words >= 4:
            flush()
        elif words >= 36:
            flush()
    flush(force=True)
    return chunks


def _looks_complete_sentence(text: str) -> bool:
    cleaned = " ".join((text or "").split()).strip()
    if not cleaned:
        return False
    words = cleaned.split()
    if len(words) < 3:
        return False
    if _SENTENCE_END_RE.search(cleaned):
        return True
    # Spoken captions often omit terminal punctuation; require enough context.
    return len(words) >= 6


def validate_english_sentences(sentences: list[TimedSentence]) -> None:
    """Raise if any segment is empty, incomplete, or not English."""
    if not sentences:
        raise EnglishTranscriptError(MSG_INCOMPLETE)
    for i, row in enumerate(sentences):
        text = " ".join((row.text or "").split()).strip()
        if not text:
            raise EnglishTranscriptError(MSG_INCOMPLETE)
        if not _looks_complete_sentence(text):
            raise EnglishTranscriptError(MSG_INCOMPLETE)
        if not is_english_text(text):
            raise EnglishTranscriptError(MSG_NOT_ENGLISH)
        if row.end_sec is not None and float(row.end_sec) + 1e-6 < float(row.start_sec):
            raise EnglishTranscriptError(MSG_TIMESTAMP_BAD)


async def _translate_sentences_llm(
    sentences: list[TimedSentence],
    *,
    provider: str = "auto",
    model: str | None = None,
) -> tuple[list[TimedSentence], str]:
    """Faithful English translation; preserves timestamps; fails closed on gaps."""
    from app.llm.carousel_llm import resolve_carousel_llm
    from app.search.carousel_pipeline import _llm_complete_json, _llm_has_any_key

    pack = resolve_carousel_llm(provider=provider, model=model)
    if not _llm_has_any_key(
        api_key=pack.get("api_key"),
        claude_api_key=pack.get("claude_api_key"),
        openrouter_api_key=pack.get("openrouter_api_key"),
        openrouter_model=pack.get("openrouter_model") or "",
    ):
        raise EnglishTranscriptError(MSG_NO_LLM_KEY)

    out: list[TimedSentence] = []
    used_provider = "unknown"

    for batch_start in range(0, len(sentences), _BATCH_SIZE):
        batch = sentences[batch_start : batch_start + _BATCH_SIZE]
        numbered = [{"i": i, "text": s.text} for i, s in enumerate(batch)]
        prompt = (
            "Translate each video transcript segment into English.\n"
            "Rules:\n"
            "- Faithful translation ONLY — do not summarize, paraphrase beyond "
            "translation, shorten, invent, or omit meaning.\n"
            "- Do NOT alter sentence structure beyond what translation requires.\n"
            "- Keep each item as ONE complete sentence (or the same complete "
            "utterance as the source).\n"
            "- Preserve proper nouns that are already Latin-script names.\n"
            "- Do NOT merge or split across items; one output per input index.\n"
            "- If you cannot translate an item confidently, set \"text\" to null "
            "for that item (do not guess).\n"
            "- Return ONLY JSON: {\"segments\": [{\"i\": 0, \"text\": \"...\"}, ...]}.\n\n"
            f"Segments:\n{json.dumps(numbered, ensure_ascii=False)}"
        )
        try:
            raw_text, used_provider = await _llm_complete_json(
                prompt=prompt,
                system=(
                    "You are a precise transcript translator. "
                    "Return ONLY valid JSON. Never invent content."
                ),
                temperature=0.1,
                max_tokens=min(8192, 400 + 120 * len(batch)),
                api_key=pack.get("api_key"),
                model=pack.get("model") or "",
                claude_api_key=pack.get("claude_api_key"),
                claude_model=pack.get("claude_model") or "",
                provider=pack.get("provider") or "auto",
                openrouter_api_key=pack.get("openrouter_api_key"),
                openrouter_model=pack.get("openrouter_model") or "",
                openrouter_base_url=pack.get("openrouter_base_url") or "",
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("LLM translation failed: %s", str(exc)[:240])
            raise EnglishTranscriptError(MSG_TRANSLATE_FAILED) from exc

        by_i = _parse_translation_json(raw_text, expected=len(batch))
        for i, src in enumerate(batch):
            eng = by_i.get(i)
            if eng is None:
                logger.warning(
                    "Missing translation for segment %d @ %.1fs",
                    batch_start + i,
                    src.start_sec,
                )
                raise EnglishTranscriptError(MSG_TRANSLATE_FAILED)
            eng = " ".join(eng.split()).strip()
            if not eng:
                raise EnglishTranscriptError(MSG_TRANSLATE_FAILED)
            if not is_english_text(eng):
                raise EnglishTranscriptError(MSG_NOT_ENGLISH)
            out.append(
                TimedSentence(
                    start_sec=src.start_sec,
                    end_sec=src.end_sec,
                    text=eng,
                )
            )

    return out, used_provider


def _parse_translation_json(raw: str, *, expected: int) -> dict[int, str | None]:
    text = (raw or "").strip()
    if not text:
        raise EnglishTranscriptError(MSG_TRANSLATE_FAILED)
    data: Any
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{[\s\S]*\}", text) or re.search(r"\[[\s\S]*\]", text)
        if not m:
            raise EnglishTranscriptError(MSG_TRANSLATE_FAILED) from None
        try:
            data = json.loads(m.group())
        except json.JSONDecodeError as exc:
            raise EnglishTranscriptError(MSG_TRANSLATE_FAILED) from exc

    rows: list[Any]
    if isinstance(data, dict):
        rows = data.get("segments") or data.get("lines") or data.get("items") or []
        if not isinstance(rows, list):
            raise EnglishTranscriptError(MSG_TRANSLATE_FAILED)
    elif isinstance(data, list):
        rows = data
    else:
        raise EnglishTranscriptError(MSG_TRANSLATE_FAILED)

    by_i: dict[int, str | None] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        try:
            i = int(row.get("i"))
        except (TypeError, ValueError):
            continue
        if i < 0 or i >= expected:
            continue
        if row.get("text") is None:
            by_i[i] = None
            continue
        val = str(row.get("text") or "").strip()
        by_i[i] = val if val else None
    return by_i


async def _load_text_cues(
    session: AsyncSession,
    media_id: int,
) -> list[VideoSegment]:
    rows = (
        await session.execute(
            select(VideoSegment)
            .where(VideoSegment.media_id == media_id, VideoSegment.text != "")
            .order_by(VideoSegment.start_sec)
        )
    ).scalars().all()
    return list(rows)


def _nearest_frame_path(
    start_sec: float,
    frame_by_start: list[tuple[float, str | None]],
) -> str | None:
    if not frame_by_start:
        return None
    best = min(frame_by_start, key=lambda p: abs(p[0] - start_sec))
    return best[1]


async def _replace_text_segments(
    session: AsyncSession,
    *,
    media: Media,
    drive_file_id: str,
    sentences: list[TimedSentence],
    old_segments: list[VideoSegment],
) -> int:
    """Delete prior text cues, insert English sentences, refresh Qdrant."""
    frame_by_start = [
        (float(s.start_sec or 0.0), s.frame_path)
        for s in old_segments
        if s.frame_path
    ]
    deleted = 0
    for seg in old_segments:
        # Keep frame-only rows (empty text) for face/frame indexes.
        if (seg.text or "").strip():
            await session.delete(seg)
            deleted += 1
    await session.flush()

    new_rows: list[VideoSegment] = []
    for sentence in sentences:
        new_rows.append(
            VideoSegment(
                media_id=media.id,
                start_sec=float(sentence.start_sec),
                end_sec=float(sentence.end_sec) if sentence.end_sec is not None else None,
                text=sentence.text,
                language="en",
                frame_path=_nearest_frame_path(sentence.start_sec, frame_by_start),
            )
        )
        session.add(new_rows[-1])
    await session.flush()

    await _refresh_qdrant_transcripts(drive_file_id=drive_file_id, segments=new_rows)
    return deleted


async def _refresh_qdrant_transcripts(
    *,
    drive_file_id: str,
    segments: list[VideoSegment],
) -> None:
    import asyncio

    from app.config import get_settings
    from app.gemini.video_embeddings import embed_text_sync
    from app.qdrant.video_transcripts import (
        delete_transcripts_for_file_sync,
        upsert_transcript_segment_sync,
    )

    settings = get_settings()
    try:
        await asyncio.to_thread(delete_transcripts_for_file_sync, drive_file_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Qdrant transcript clear failed for %s: %s", drive_file_id, exc)
        raise EnglishTranscriptError(MSG_QDRANT_FAILED) from exc

    if not (settings.gemini_api_key or "").strip():
        logger.warning(
            "Skipping Qdrant transcript re-index for %s (no gemini_api_key)",
            drive_file_id,
        )
        return

    sem = asyncio.Semaphore(settings.gemini_embed_max_concurrent)

    async def _one(seg: VideoSegment) -> None:
        text = (seg.text or "").strip()
        if len(text) < 8:
            return
        async with sem:
            vec = await asyncio.to_thread(embed_text_sync, text[:2000])
            await asyncio.to_thread(
                upsert_transcript_segment_sync,
                drive_file_id=drive_file_id,
                start_sec=float(seg.start_sec or 0.0),
                end_sec=float(seg.end_sec) if seg.end_sec is not None else None,
                text=text,
                vector=vec,
            )

    try:
        await asyncio.gather(*(_one(s) for s in segments))
    except Exception as exc:  # noqa: BLE001
        logger.warning("Qdrant transcript re-index failed for %s: %s", drive_file_id, exc)
        raise EnglishTranscriptError(MSG_QDRANT_FAILED) from exc


async def ensure_english_transcript(
    session: AsyncSession,
    drive_file_id: str,
    *,
    provider: str = "auto",
    model: str | None = None,
    force: bool = False,
) -> EnglishTranscriptResult:
    """
    Ensure the video's stored transcript is English complete sentences.

    YouTube and Drive videos are both addressed by ``drive_file_id``.
    Raises ``EnglishTranscriptError`` on processing difficulty (no false results).
    """
    file_id = (drive_file_id or "").strip()
    if not file_id:
        raise EnglishTranscriptError(MSG_VIDEO_MISSING)

    drive_file = await session.get(DriveFile, file_id)
    if drive_file is None:
        raise EnglishTranscriptError(MSG_VIDEO_MISSING)

    media = (
        await session.execute(select(Media).where(Media.drive_file_id == file_id))
    ).scalar_one_or_none()
    if media is None:
        media = Media(drive_file_id=file_id, type=MediaType.VIDEO)
        session.add(media)
        await session.flush()

    old_segments = await _load_text_cues(session, media.id)

    # Try YouTube captions when nothing is stored yet.
    if not old_segments:
        from app.video.transcript_ingest import ingest_youtube_transcript_for_drive_file

        try:
            n = await ingest_youtube_transcript_for_drive_file(
                session, drive_file, force=False
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("YouTube caption fetch failed for %s: %s", file_id, exc)
            raise EnglishTranscriptError(MSG_NO_TRANSCRIPT) from exc
        if n:
            old_segments = await _load_text_cues(session, media.id)

    if not old_segments:
        raise EnglishTranscriptError(MSG_NO_TRANSCRIPT)

    # Already tagged English and not forced → return as-is after light validation.
    if (
        not force
        and all((s.language or "").lower().startswith("en") for s in old_segments)
        and not cues_need_english((s.start_sec, s.end_sec, s.text) for s in old_segments)
    ):
        sentences = [
            TimedSentence(
                start_sec=float(s.start_sec),
                end_sec=float(s.end_sec) if s.end_sec is not None else None,
                text=" ".join((s.text or "").split()).strip(),
            )
            for s in old_segments
            if (s.text or "").strip()
        ]
        try:
            validate_english_sentences(sentences)
        except EnglishTranscriptError:
            # Stored as en but incomplete / fragmented → restitch below.
            pass
        else:
            return EnglishTranscriptResult(
                drive_file_id=file_id,
                media_id=media.id,
                cue_count=len(sentences),
                translated=False,
                already_english=True,
                language="en",
                source="stored_en",
                segments=sentences,
                message=f"{MSG_ALREADY_ENGLISH} ({len(sentences)} sentences).",
            )

    cue_tuples = [
        (float(s.start_sec), float(s.end_sec) if s.end_sec is not None else None, s.text or "")
        for s in old_segments
    ]
    stitched = stitch_complete_sentences(cue_tuples)
    if not stitched:
        raise EnglishTranscriptError(MSG_INCOMPLETE)

    needs_translate = cues_need_english(
        (s.start_sec, s.end_sec, s.text) for s in stitched
    )
    llm_provider: str | None = None
    source = "stitched_en"

    if needs_translate:
        translated, llm_provider = await _translate_sentences_llm(
            stitched,
            provider=provider,
            model=model,
        )
        sentences = translated
        source = "llm_translate"
        translated_flag = True
        already_english = False
    else:
        # English already — keep original wording; only stitch for completeness.
        sentences = stitched
        translated_flag = False
        already_english = True
        source = "stitched_en"

    validate_english_sentences(sentences)

    deleted = await _replace_text_segments(
        session,
        media=media,
        drive_file_id=file_id,
        sentences=sentences,
        old_segments=old_segments,
    )

    logger.info(
        "English transcript ready for %s: cues=%d translated=%s source=%s deleted=%d",
        file_id,
        len(sentences),
        translated_flag,
        source,
        deleted,
    )
    if translated_flag:
        user_msg = f"{MSG_TRANSLATED} ({len(sentences)} sentences)."
    elif already_english:
        user_msg = f"{MSG_ALREADY_ENGLISH} ({len(sentences)} sentences)."
    else:
        user_msg = f"{MSG_STITCHED} ({len(sentences)} sentences)."
    return EnglishTranscriptResult(
        drive_file_id=file_id,
        media_id=media.id,
        cue_count=len(sentences),
        translated=translated_flag,
        already_english=already_english,
        language="en",
        source=source,
        llm_provider=llm_provider,
        deleted_non_english=deleted if needs_translate else 0,
        segments=sentences,
        message=user_msg,
    )


async def find_non_english_transcript_videos(
    session: AsyncSession,
    *,
    limit: int = 500,
) -> list[dict[str, Any]]:
    """List videos whose stored cue text is predominantly non-English."""
    from sqlalchemy import func

    cue_count = func.count(VideoSegment.id).label("cue_count")
    rows = (
        await session.execute(
            select(DriveFile.id, DriveFile.name, Media.id, cue_count)
            .join(Media, Media.drive_file_id == DriveFile.id)
            .join(VideoSegment, VideoSegment.media_id == Media.id)
            .where(VideoSegment.text != "")
            .group_by(DriveFile.id, DriveFile.name, Media.id)
            .having(cue_count > 0)
            .order_by(DriveFile.name)
            .limit(max(1, min(limit, 2000)))
        )
    ).all()

    out: list[dict[str, Any]] = []
    for drive_id, name, media_id, _count in rows:
        segs = await _load_text_cues(session, int(media_id))
        if not segs:
            continue
        # Explicit non-en language tag, or heuristic on text.
        tagged_non_en = any(
            (s.language or "").strip()
            and not (s.language or "").lower().startswith("en")
            for s in segs
        )
        heuristic = cues_need_english(
            (s.start_sec, s.end_sec, s.text) for s in segs
        )
        if not (tagged_non_en or heuristic):
            continue
        sample = " ".join((s.text or "") for s in segs[:3])[:160]
        out.append(
            {
                "drive_file_id": drive_id,
                "name": name,
                "media_id": int(media_id),
                "cue_count": len(segs),
                "sample": sample,
            }
        )
    return out


async def purge_non_english_transcripts(
    session: AsyncSession,
    *,
    dry_run: bool = True,
    translate: bool = True,
    provider: str = "auto",
    model: str | None = None,
    limit: int = 200,
) -> dict[str, Any]:
    """
    Delete stored non-English transcripts.

    When ``translate=True`` (default), replace each with an English transcript
    via ``ensure_english_transcript``. When ``translate=False``, only delete
    text segments + Qdrant points (user must re-run ensure later).
    """
    import asyncio

    from app.qdrant.video_transcripts import delete_transcripts_for_file_sync

    candidates = await find_non_english_transcript_videos(session, limit=limit)
    if dry_run:
        return {
            "ok": True,
            "dry_run": True,
            "count": len(candidates),
            "videos": candidates,
        }

    results: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    for row in candidates:
        file_id = str(row["drive_file_id"])
        try:
            if translate:
                result = await ensure_english_transcript(
                    session,
                    file_id,
                    provider=provider,
                    model=model,
                    force=True,
                )
                await session.commit()
                results.append(
                    {
                        "drive_file_id": file_id,
                        "cue_count": result.cue_count,
                        "translated": result.translated,
                        "source": result.source,
                    }
                )
            else:
                media_id = int(row["media_id"])
                segs = await _load_text_cues(session, media_id)
                for seg in segs:
                    await session.delete(seg)
                await session.flush()
                await asyncio.to_thread(delete_transcripts_for_file_sync, file_id)
                await session.commit()
                results.append(
                    {
                        "drive_file_id": file_id,
                        "cue_count": 0,
                        "translated": False,
                        "source": "deleted_only",
                    }
                )
        except EnglishTranscriptError as exc:
            await session.rollback()
            errors.append({"drive_file_id": file_id, "error": str(exc)})
        except Exception as exc:  # noqa: BLE001
            await session.rollback()
            errors.append({"drive_file_id": file_id, "error": str(exc)[:240]})

    return {
        "ok": len(errors) == 0,
        "dry_run": False,
        "processed": len(results),
        "failed": len(errors),
        "results": results,
        "errors": errors,
    }
