"""
Append-only body/clothing re-identification layer.

Pipeline (proved boxes first, then Gemini):
  1. Ultralytics YOLOv8n detects person bounding boxes (COCO class 0).
  2. Each indexed face is linked to the smallest person box containing its center.
  3. That person crop is Gemini-embedded into `body_signatures`.

Annotated proof JPEGs are saved under thumbnail_dir/reid_proof/.
"""

from __future__ import annotations

import asyncio
import logging
import os
import tempfile
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.models import BodySignature, DriveFile, Face, Media, MediaType, Person
from app.dependencies import get_drive_client
from app.gemini.video_embeddings import embed_frame_sync
from app.pipelines.common import (
    body_crop_path,
    decode_image_bgr,
    download_to_memory,
    save_body_crop_thumbnail,
)
from app.reid.person_detect import (
    PersonBox,
    best_person_for_face,
    detect_persons_bgr,
    draw_proof,
    yolov8_available,
)

logger = logging.getLogger(__name__)

_FULL_BODY_MIN_HEIGHT_FRAC = 0.35  # person box must cover ≥35% of image height
_FULL_BODY_MIN_AREA_FRAC = 0.04    # person box ≥4% of image area


def _embed_jpeg_bytes(jpeg_bytes: bytes) -> list[float]:
    """Gemini-embed raw JPEG bytes (sync — call via to_thread)."""
    settings = get_settings()
    os.makedirs(settings.temp_dir, exist_ok=True)
    fd, path = tempfile.mkstemp(dir=settings.temp_dir, prefix="body_", suffix=".jpg")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(jpeg_bytes)
        return embed_frame_sync(path)
    finally:
        if os.path.exists(path):
            os.remove(path)


def _crop_jpeg(image_bgr, box: BodyBox) -> bytes | None:
    import cv2

    x0, y0 = int(box.x), int(box.y)
    x1, y1 = int(box.x + box.width), int(box.y + box.height)
    crop = image_bgr[y0:y1, x0:x1]
    if crop.size == 0:
        return None
    ok, buf = cv2.imencode(".jpg", crop, [int(cv2.IMWRITE_JPEG_QUALITY), 90])
    return buf.tobytes() if ok else None


@dataclass
class BodyBox:
    x: float
    y: float
    width: float
    height: float
    coverage: float
    prominence: float
    confidence: float = 1.0
    backend: str = "yolov8n"


def _person_to_body_box(person: PersonBox, img_w: int, img_h: int) -> BodyBox:
    area = max(float(img_w * img_h), 1.0)
    box_area = max(person.width * person.height, 0.0)
    height_frac = person.height / max(float(img_h), 1.0)
    return BodyBox(
        x=person.x,
        y=person.y,
        width=person.width,
        height=person.height,
        coverage=min(1.0, height_frac / 0.85),  # ~full standing person ≈ 85% frame height
        prominence=box_area / area,
        confidence=person.confidence,
        backend=person.backend,
    )


def proof_path(media_id: int, settings=None) -> str:
    settings = settings or get_settings()
    return os.path.join(settings.thumbnail_dir, "reid_proof", f"media_{media_id}.jpg")


def _save_proof_jpeg(media_id: int, annotated_bgr, settings) -> str:
    import cv2

    path = proof_path(media_id, settings)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    cv2.imwrite(path, annotated_bgr, [int(cv2.IMWRITE_JPEG_QUALITY), 88])
    return path


async def prove_media_bodies(
    session: AsyncSession,
    media_id: int,
    *,
    client=None,
    embed: bool = True,
) -> dict:
    """
    Run real person detection on one media file, draw proved bounding boxes,
    optionally Gemini-embed crops linked to faces. Returns proof stats + paths.
    """
    settings = get_settings()
    client = client or get_drive_client()
    media = await session.get(Media, media_id)
    if media is None:
        raise ValueError(f"Media {media_id} not found")
    drive_file = await session.get(DriveFile, media.drive_file_id)
    if drive_file is None:
        raise ValueError(f"Drive file for media {media_id} not found")

    raw = await download_to_memory(client, drive_file.id)
    image_bgr = await asyncio.to_thread(decode_image_bgr, raw, file_name=drive_file.name)
    img_h, img_w = image_bgr.shape[:2]

    persons = await asyncio.to_thread(detect_persons_bgr, image_bgr)
    faces = (
        await session.execute(select(Face).where(Face.media_id == media_id).order_by(Face.id))
    ).scalars().all()
    face_boxes = [(f.bbox_x, f.bbox_y, f.bbox_width, f.bbox_height) for f in faces]

    annotated = await asyncio.to_thread(draw_proof, image_bgr, persons, face_boxes)
    proof = await asyncio.to_thread(_save_proof_jpeg, media_id, annotated, settings)

    linked: list[dict] = []
    embedded = 0
    for face in faces:
        person = best_person_for_face(
            face.bbox_x, face.bbox_y, face.bbox_width, face.bbox_height, persons
        )
        if person is None:
            continue
        box = _person_to_body_box(person, img_w, img_h)
        jpeg = _crop_jpeg(image_bgr, box)
        if not jpeg:
            continue
        save_body_crop_thumbnail(face.id, jpeg, settings)
        entry = {
            "face_id": face.id,
            "person_box": {
                "x": round(box.x, 1),
                "y": round(box.y, 1),
                "w": round(box.width, 1),
                "h": round(box.height, 1),
                "confidence": round(box.confidence, 3),
                "backend": box.backend,
            },
            "has_crop": True,
            "embedded": False,
        }
        if embed:
            existing = (
                await session.execute(select(BodySignature).where(BodySignature.face_id == face.id))
            ).scalar_one_or_none()
            if existing is None:
                try:
                    vector = await asyncio.to_thread(_embed_jpeg_bytes, jpeg)
                    session.add(
                        BodySignature(
                            face_id=face.id,
                            media_id=media.id,
                            person_id=face.person_id,
                            body_x=box.x,
                            body_y=box.y,
                            body_width=box.width,
                            body_height=box.height,
                            prominence=box.prominence,
                            body_coverage=box.coverage,
                            is_full_body=(
                                box.prominence >= _FULL_BODY_MIN_AREA_FRAC
                                and (box.height / max(img_h, 1)) >= _FULL_BODY_MIN_HEIGHT_FRAC
                            ),
                            embedding=vector,
                        )
                    )
                    entry["embedded"] = True
                    embedded += 1
                except Exception as exc:  # noqa: BLE001
                    logger.warning("prove embed failed face=%s: %s", face.id, exc)
                    entry["embed_error"] = str(exc)[:120]
            else:
                entry["embedded"] = True
                entry["already_had_signature"] = True
        linked.append(entry)

    await session.commit()
    return {
        "media_id": media_id,
        "drive_file_id": drive_file.id,
        "file_name": drive_file.name,
        "image_size": {"width": img_w, "height": img_h},
        "detector": "yolov8n" if yolov8_available() else "opencv_hog",
        "yolov8_available": yolov8_available(),
        "persons_detected": len(persons),
        "faces_on_media": len(faces),
        "faces_linked_to_person_box": len(linked),
        "embedded": embedded,
        "proof_url": f"/reid/proof/{media_id}",
        "persons": [
            {
                "x": round(p.x, 1),
                "y": round(p.y, 1),
                "w": round(p.width, 1),
                "h": round(p.height, 1),
                "confidence": round(p.confidence, 3),
                "backend": p.backend,
            }
            for p in persons
        ],
        "links": linked,
    }


async def backfill_body_signatures(
    session: AsyncSession,
    *,
    limit: int = 200,
    client=None,
) -> dict:
    """
    YOLO person boxes → body crop → Gemini embed (append-only).
    Only faces whose center falls inside a detected person box are indexed.
    """
    settings = get_settings()
    client = client or get_drive_client()

    stmt = (
        select(Face, Media, DriveFile)
        .join(Media, Face.media_id == Media.id)
        .join(DriveFile, Media.drive_file_id == DriveFile.id)
        .outerjoin(BodySignature, BodySignature.face_id == Face.id)
        .where(Media.type == MediaType.IMAGE, BodySignature.id.is_(None))
        .order_by(Face.id)
        .limit(limit)
    )
    rows = (await session.execute(stmt)).all()

    stats = {
        "scanned": 0,
        "embedded": 0,
        "no_person_box": 0,
        "not_full_body": 0,
        "errors": 0,
        "detector": "yolov8n" if yolov8_available() else "opencv_hog",
    }
    by_file: dict[str, list[tuple[Face, Media, DriveFile]]] = {}
    for face, media, drive_file in rows:
        by_file.setdefault(drive_file.id, []).append((face, media, drive_file))

    for drive_file_id, entries in by_file.items():
        file_name = entries[0][2].name
        media_id = entries[0][1].id
        try:
            raw = await download_to_memory(client, drive_file_id)
            image_bgr = await asyncio.to_thread(decode_image_bgr, raw, file_name=file_name)
            persons = await asyncio.to_thread(detect_persons_bgr, image_bgr)
        except Exception as exc:  # noqa: BLE001
            logger.warning("reid: could not fetch/detect %s: %s", file_name, exc)
            stats["errors"] += len(entries)
            continue

        img_h, img_w = image_bgr.shape[:2]
        face_boxes = [(f.bbox_x, f.bbox_y, f.bbox_width, f.bbox_height) for f, _, _ in entries]
        annotated = await asyncio.to_thread(draw_proof, image_bgr, persons, face_boxes)
        await asyncio.to_thread(_save_proof_jpeg, media_id, annotated, settings)

        for face, media, _drive_file in entries:
            stats["scanned"] += 1
            person = best_person_for_face(
                face.bbox_x, face.bbox_y, face.bbox_width, face.bbox_height, persons
            )
            if person is None:
                stats["no_person_box"] += 1
                continue
            box = _person_to_body_box(person, img_w, img_h)
            height_frac = box.height / max(float(img_h), 1.0)
            if box.prominence < _FULL_BODY_MIN_AREA_FRAC or height_frac < _FULL_BODY_MIN_HEIGHT_FRAC:
                stats["not_full_body"] += 1
                # Still save crop + proof for the lab; only skip Gemini index for tiny torsos.
                jpeg = _crop_jpeg(image_bgr, box)
                if jpeg:
                    save_body_crop_thumbnail(face.id, jpeg, settings)
                continue
            jpeg = _crop_jpeg(image_bgr, box)
            if not jpeg:
                stats["errors"] += 1
                continue
            save_body_crop_thumbnail(face.id, jpeg, settings)
            try:
                vector = await asyncio.to_thread(_embed_jpeg_bytes, jpeg)
            except Exception as exc:  # noqa: BLE001
                logger.warning("reid: embed failed for face %s: %s", face.id, exc)
                stats["errors"] += 1
                continue
            session.add(
                BodySignature(
                    face_id=face.id,
                    media_id=media.id,
                    person_id=face.person_id,
                    body_x=box.x,
                    body_y=box.y,
                    body_width=box.width,
                    body_height=box.height,
                    prominence=box.prominence,
                    body_coverage=box.coverage,
                    is_full_body=True,
                    embedding=vector,
                )
            )
            stats["embedded"] += 1
        await session.commit()

    return stats


async def body_identification_candidates(
    session: AsyncSession,
    *,
    limit: int = 50,
    threshold: float | None = None,
) -> list[dict]:
    """
    Further identification layer: for unlabeled faces with a body signature,
    propose the nearest named person by clothing/body similarity. A same-folder
    hit adds a small boost (same shoot → same outfit is far more likely).
    """
    settings = get_settings()
    threshold = threshold if threshold is not None else settings.reid_body_match_threshold

    unlabeled = (
        (
            await session.execute(
                select(BodySignature)
                .where(BodySignature.person_id.is_(None))
                .order_by(BodySignature.id.desc())
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )

    candidates: list[dict] = []
    for sig in unlabeled:
        dist = BodySignature.embedding.cosine_distance(sig.embedding).label("dist")
        nearest = (
            await session.execute(
                select(BodySignature, dist)
                .where(BodySignature.person_id.is_not(None))
                .order_by(dist)
                .limit(1)
            )
        ).first()
        if nearest is None:
            continue
        match_sig, distance = nearest
        similarity = 1.0 - float(distance)
        if similarity < threshold:
            continue

        person = await session.get(Person, match_sig.person_id)
        src_path = await _file_path_for_media(session, sig.media_id)
        match_path = await _file_path_for_media(session, match_sig.media_id)
        same_folder = bool(
            src_path and match_path and os.path.dirname(src_path) == os.path.dirname(match_path)
        )
        candidates.append(
            {
                "face_id": sig.face_id,
                "matched_face_id": match_sig.face_id,
                "person_id": match_sig.person_id,
                "person_name": person.name if person else None,
                "body_similarity": round(similarity, 4),
                "same_folder": same_folder,
                "combined_score": round(min(similarity + (0.05 if same_folder else 0.0), 1.0), 4),
                "source_path": src_path,
                "matched_path": match_path,
                "is_full_body": sig.is_full_body,
            }
        )

    candidates.sort(key=lambda c: c["combined_score"], reverse=True)
    return candidates


async def refresh_signature_person_links(session: AsyncSession) -> int:
    """Propagate person labels onto signatures whose faces got named after backfill."""
    stmt = (
        select(BodySignature, Face)
        .join(Face, BodySignature.face_id == Face.id)
        .where(BodySignature.person_id.is_(None), Face.person_id.is_not(None))
    )
    rows = (await session.execute(stmt)).all()
    for sig, face in rows:
        sig.person_id = face.person_id
    await session.commit()
    return len(rows)


async def _file_path_for_media(session: AsyncSession, media_id: int) -> str | None:
    row = (
        await session.execute(
            select(DriveFile.path)
            .join(Media, Media.drive_file_id == DriveFile.id)
            .where(Media.id == media_id)
        )
    ).scalar_one_or_none()
    return row


async def body_gallery(session: AsyncSession, *, limit: int = 48) -> list[dict]:
    """Visual lab payload: body signatures + labels + nearest match hints."""
    await refresh_signature_person_links(session)
    candidates = await body_identification_candidates(session, limit=limit)
    cand_by_face = {c["face_id"]: c for c in candidates}

    rows = (
        await session.execute(
            select(BodySignature, Face, Media, DriveFile, Person)
            .join(Face, BodySignature.face_id == Face.id)
            .join(Media, BodySignature.media_id == Media.id)
            .join(DriveFile, Media.drive_file_id == DriveFile.id)
            .outerjoin(Person, BodySignature.person_id == Person.id)
            .order_by(BodySignature.id.desc())
            .limit(limit)
        )
    ).all()

    out: list[dict] = []
    for sig, face, media, drive_file, person in rows:
        cand = cand_by_face.get(face.id)
        out.append(
            {
                "signature_id": sig.id,
                "face_id": face.id,
                "person_id": sig.person_id,
                "person_name": person.name if person else None,
                "drive_file_id": drive_file.id,
                "file_name": drive_file.name,
                "file_path": drive_file.path,
                "mime_type": drive_file.mime_type,
                "prominence_pct": round(sig.prominence * 100, 1),
                "body_coverage_pct": round(sig.body_coverage * 100, 1),
                "is_full_body": sig.is_full_body,
                "has_body_crop": os.path.exists(body_crop_path(face.id)),
                "has_face_thumb": bool(face.thumbnail_path),
                "has_proof": os.path.exists(proof_path(media.id)),
                "proof_url": f"/reid/proof/{media.id}" if os.path.exists(proof_path(media.id)) else None,
                "candidate": cand,
            }
        )
    return out
