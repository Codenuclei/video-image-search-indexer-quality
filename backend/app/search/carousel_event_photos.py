"""Face-matched event photos for carousel slides.

The visible speaker identity comes from the source video's quote-window
catalog. Candidates are indexed image faces from the manually linked Drive
root only. Low-confidence matches are omitted instead of guessing.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from pathlib import Path
from typing import Any

from PIL import Image, ImageOps
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.db.models import DriveFile, Face, FaceCluster, FaceEmbedding, Media, Person
from app.drive.image_thumbs import image_thumb_path
from app.drive.media_cache import resolve_cache_path
from app.search.carousel_identity_catalog import has_explicit_picker_selection

logger = logging.getLogger(__name__)

EVENT_PHOTO_MATCH_VERSION = "event-photo-v1"
EVENT_PHOTO_MIN_SIMILARITY = 0.62
EVENT_PHOTO_MAX_CANDIDATES = 4
_PORTRAIT_SIZE = (1080, 1350)


def event_photo_variant_path(
    settings: Settings,
    drive_file_id: str,
) -> Path:
    safe_key = hashlib.sha256(drive_file_id.encode("utf-8")).hexdigest()[:32]
    return Path(settings.thumbnail_dir) / "carousel_event_photos" / f"{safe_key}.4x5.jpg"


def ensure_event_photo_variant(
    drive_file: DriveFile,
    settings: Settings | None = None,
) -> Path | None:
    """Materialize a cache-backed 4:5 JPEG without remote Drive I/O."""
    settings = settings or get_settings()
    source = resolve_cache_path(settings, drive_file)
    if source is None:
        thumb = image_thumb_path(settings, drive_file.id)
        source = thumb if thumb.is_file() and thumb.stat().st_size > 0 else None
    if source is None:
        return None

    dest = event_photo_variant_path(settings, drive_file.id)
    try:
        if dest.is_file() and dest.stat().st_mtime >= source.stat().st_mtime:
            return dest
        with Image.open(source) as raw:
            image = ImageOps.exif_transpose(raw).convert("RGB")
            width, height = image.size
            if width <= 0 or height <= 0:
                return None
            target_aspect = _PORTRAIT_SIZE[0] / _PORTRAIT_SIZE[1]
            if width / height > target_aspect:
                crop_width = max(1, round(height * target_aspect))
                left = max(0, (width - crop_width) // 2)
                image = image.crop((left, 0, left + crop_width, height))
            else:
                crop_height = max(1, round(width / target_aspect))
                top = max(0, round((height - crop_height) * 0.33))
                image = image.crop((0, top, width, top + crop_height))
            image.thumbnail(_PORTRAIT_SIZE, Image.Resampling.LANCZOS)
            dest.parent.mkdir(parents=True, exist_ok=True)
            partial = dest.with_suffix(".partial.jpg")
            image.save(partial, "JPEG", quality=90, optimize=True)
            partial.replace(dest)
        return dest
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "event photo derivative failed file=%s error=%s",
            drive_file.id,
            str(exc)[:160],
        )
        return None


def _catalog_identity_centroid(
    catalog: dict[str, Any],
    identity_id: str,
) -> list[float] | None:
    for identity in catalog.get("identities") or []:
        if not isinstance(identity, dict) or str(identity.get("id") or "") != identity_id:
            continue
        centroid = identity.get("centroid")
        if isinstance(centroid, list) and len(centroid) == 512:
            return [float(value) for value in centroid]
    return None


async def match_event_photos(
    session: AsyncSession,
    *,
    folder_id: str,
    query_embedding: list[float],
    settings: Settings | None = None,
    limit: int = EVENT_PHOTO_MAX_CANDIDATES,
    min_similarity: float = EVENT_PHOTO_MIN_SIMILARITY,
) -> list[dict[str, Any]]:
    """Rank one best matching face per indexed image inside ``folder_id``."""
    if len(query_embedding) != 512:
        return []
    settings = settings or get_settings()
    distance = FaceEmbedding.embedding.cosine_distance(query_embedding).label("distance")
    identity_person_id = func.coalesce(Face.person_id, FaceCluster.person_id).label(
        "identity_person_id"
    )
    rows = (
        await session.execute(
            select(
                DriveFile,
                Face,
                distance,
                identity_person_id,
                Person.name,
            )
            .select_from(FaceEmbedding)
            .join(Face, Face.id == FaceEmbedding.face_id)
            .join(Media, Media.id == Face.media_id)
            .join(DriveFile, DriveFile.id == Media.drive_file_id)
            .outerjoin(FaceCluster, FaceCluster.id == Face.cluster_id)
            .outerjoin(Person, Person.id == identity_person_id)
            .where(
                DriveFile.root_folder_id == folder_id,
                DriveFile.mime_type.like("image/%"),
                DriveFile.archived_at.is_(None),
            )
            .order_by(distance, Face.detection_confidence.desc(), DriveFile.id)
            .limit(max(40, limit * 12))
        )
    ).all()

    best_by_file: dict[str, tuple[DriveFile, Face, float, int | None, str | None]] = {}
    for drive_file, face, raw_distance, person_id, person_name in rows:
        similarity = 1.0 - float(raw_distance)
        if similarity < min_similarity:
            continue
        prior = best_by_file.get(drive_file.id)
        if prior is None or similarity > prior[2]:
            best_by_file[drive_file.id] = (
                drive_file,
                face,
                similarity,
                person_id,
                person_name,
            )

    ranked = sorted(
        best_by_file.values(),
        key=lambda item: (
            item[2],
            float(item[1].detection_confidence or 0),
            float(item[1].bbox_width or 0) * float(item[1].bbox_height or 0),
        ),
        reverse=True,
    )
    candidates: list[dict[str, Any]] = []
    for drive_file, face, similarity, person_id, person_name in ranked:
        if len(candidates) >= max(1, limit):
            break
        derivative = await asyncio.to_thread(
            ensure_event_photo_variant,
            drive_file,
            settings,
        )
        if derivative is None:
            continue
        candidates.append(
            {
                "asset_type": "event_photo",
                "photo_drive_file_id": drive_file.id,
                "preview_url": f"/media/event-photo/{drive_file.id}",
                "label": "Event photo",
                "category": "event_photo",
                "similarity": round(similarity, 4),
                "detection_confidence": round(float(face.detection_confidence or 0), 4),
                "identity_person_id": person_id,
                "identity_person_name": person_name,
                "selected": False,
                "recommended": len(candidates) == 0,
                "recommendation_source": "event_photo_face_match",
            }
        )
    for order, candidate in enumerate(candidates):
        candidate["order"] = order
    return candidates


async def apply_event_photo_matches(
    session: AsyncSession,
    slides: list[dict[str, Any]],
    *,
    folder_id: str,
    catalog: dict[str, Any],
    settings: Settings | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Prepend folder matches and retain video frames as fallback candidates."""
    settings = settings or get_settings()
    out: list[dict[str, Any]] = []
    matched_slides = 0
    cache: dict[str, list[dict[str, Any]]] = {}
    for slide_index, raw_slide in enumerate(slides):
        slide = dict(raw_slide)
        slide["layout_role"] = "cover" if slide_index == 0 else "body"
        if has_explicit_picker_selection(slide):
            out.append(slide)
            continue
        association = slide.get("identity_association") or {}
        mode = str(association.get("mode") or "")
        identity_id = str(association.get("identity_id") or "")
        centroid = _catalog_identity_centroid(catalog, identity_id) if identity_id else None
        event_items: list[dict[str, Any]] = []
        if centroid is not None and mode == "speaker":
            if identity_id not in cache:
                cache[identity_id] = await match_event_photos(
                    session,
                    folder_id=folder_id,
                    query_embedding=centroid,
                    settings=settings,
                )
            event_items = [dict(item) for item in cache[identity_id]]
        elif mode == "group_panel":
            # Prefer multi-face event photos that still match any window identity,
            # but never invent a speaker from caption text alone.
            group_pool: list[dict[str, Any]] = []
            for identity in catalog.get("identities") or []:
                if not isinstance(identity, dict):
                    continue
                iid = str(identity.get("id") or "")
                emb = identity.get("centroid")
                if not iid or not isinstance(emb, list) or len(emb) != 512:
                    continue
                if iid not in cache:
                    cache[iid] = await match_event_photos(
                        session,
                        folder_id=folder_id,
                        query_embedding=[float(v) for v in emb],
                        settings=settings,
                        limit=2,
                    )
                for item in cache[iid]:
                    clone = dict(item)
                    clone["category"] = "group_event_photo"
                    clone["label"] = "Event group photo"
                    clone["recommended"] = False
                    group_pool.append(clone)
            # Deduplicate by Drive photo id while keeping best similarity.
            by_id: dict[str, dict[str, Any]] = {}
            for item in group_pool:
                key = str(item.get("photo_drive_file_id") or "")
                prior = by_id.get(key)
                if prior is None or float(item.get("similarity") or 0) > float(
                    prior.get("similarity") or 0
                ):
                    by_id[key] = item
            event_items = sorted(
                by_id.values(),
                key=lambda item: float(item.get("similarity") or 0),
                reverse=True,
            )[:EVENT_PHOTO_MAX_CANDIDATES]
            for order, item in enumerate(event_items):
                item["order"] = order
                item["recommended"] = order == 0
        fallback_items = [
            dict(item)
            for item in (slide.get("frame_candidate_items") or [])
            if isinstance(item, dict)
        ]
        if event_items:
            matched_slides += 1
            for item in fallback_items:
                item["recommended"] = False
            combined = event_items + fallback_items
            for order, item in enumerate(combined):
                item["order"] = order
            slide["frame_candidate_items"] = combined
            slide["frame_source"] = "event_photo"
            slide["event_photo_folder_id"] = folder_id
            # Body slides get two distinct recommended panels for the MU stack.
            if slide_index > 0 and len(event_items) >= 2:
                slide["panels"] = [
                    {
                        "preview_url": event_items[0]["preview_url"],
                        "photo_drive_file_id": event_items[0].get("photo_drive_file_id"),
                        "asset_type": "event_photo",
                        "selected": False,
                        "recommended": True,
                    },
                    {
                        "preview_url": event_items[1]["preview_url"],
                        "photo_drive_file_id": event_items[1].get("photo_drive_file_id"),
                        "asset_type": "event_photo",
                        "selected": False,
                        "recommended": True,
                    },
                ]
        # Text-first remains the default; recommendations are not selections.
        slide["preview_url"] = None
        slide["frame_ts"] = None
        out.append(slide)
    return out, {
        "algorithm": EVENT_PHOTO_MATCH_VERSION,
        "folder_id": folder_id,
        "matched_slides": matched_slides,
        "slides": len(out),
        "min_similarity": EVENT_PHOTO_MIN_SIMILARITY,
    }
