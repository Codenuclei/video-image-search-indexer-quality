"""Idempotent ingest of the startups catalog into Postgres + face/logo indexes."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from pathlib import Path

import cv2
import numpy as np
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.models import (
    ClusterStatus,
    FaceCluster,
    Startup,
    StartupFaceCluster,
    StartupFounder,
    StartupLogoRef,
)
from app.matching.service import _find_best_cluster, cosine_similarity
from app.startups.catalog import CatalogStartup, load_catalog, reload_catalog
from app.startups.paths import asset_path

logger = logging.getLogger(__name__)


@dataclass
class IngestStats:
    startups_upserted: int = 0
    founders_upserted: int = 0
    face_links_created: int = 0
    face_links_existing: int = 0
    clusters_created: int = 0
    logo_refs_upserted: int = 0
    logo_refs_skipped: int = 0
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "startups_upserted": self.startups_upserted,
            "founders_upserted": self.founders_upserted,
            "face_links_created": self.face_links_created,
            "face_links_existing": self.face_links_existing,
            "clusters_created": self.clusters_created,
            "logo_refs_upserted": self.logo_refs_upserted,
            "logo_refs_skipped": self.logo_refs_skipped,
            "errors": self.errors,
        }


def _load_bgr(path: Path) -> np.ndarray | None:
    data = path.read_bytes()
    arr = np.frombuffer(data, dtype=np.uint8)
    return cv2.imdecode(arr, cv2.IMREAD_COLOR)


async def _detect_faces(image_bgr: np.ndarray) -> list:
    from app.faces.runpod_gpu import detect_faces_runpod, runpod_face_configured

    settings = get_settings()
    if runpod_face_configured(settings):
        return await detect_faces_runpod(
            image_bgr, drive_file_id="startup_catalog", settings=settings
        )
    from app.faces.engine import get_face_engine
    from app.pipelines.async_cpu import run_cpu_bound

    engine = get_face_engine()
    return await run_cpu_bound(engine.detect_faces, image_bgr)


def flatten_logo(image: np.ndarray) -> np.ndarray:
    """Composite a (possibly transparent) logo onto a contrasting padded canvas.

    Dropping alpha turns transparent pixels black, so dark-on-transparent marks
    collapse into near-identical black frames that all embed the same.
    """
    if image.ndim == 2:
        image = cv2.cvtColor(image, cv2.COLOR_GRAY2BGR)
    if image.shape[2] == 4:
        bgr = image[:, :, :3].astype(np.float32)
        alpha = image[:, :, 3:4].astype(np.float32) / 255.0
        opaque = alpha[:, :, 0] > 0.5
        luminance = float(cv2.cvtColor(image[:, :, :3], cv2.COLOR_BGR2GRAY)[opaque].mean()) if opaque.any() else 0.0
        background = 32.0 if luminance > 200 else 255.0
        image = (bgr * alpha + background * (1.0 - alpha)).clip(0, 255).astype(np.uint8)
    else:
        image = image[:, :, :3]
    h, w = image.shape[:2]
    side = int(max(h, w) * 1.2)
    fill = int(image[0, 0].mean())
    canvas = np.full((side, side, 3), fill, dtype=np.uint8)
    y, x = (side - h) // 2, (side - w) // 2
    canvas[y : y + h, x : x + w] = image
    return canvas


def _embed_logo_bytes(path: Path) -> list[float]:
    """Embed a logo image as Gemini Embedding 2 (3072-d)."""
    from app.gemini.video_embeddings import embed_frames_batch_bytes_sync

    raw = path.read_bytes()
    decoded = cv2.imdecode(np.frombuffer(raw, dtype=np.uint8), cv2.IMREAD_UNCHANGED)
    if decoded is None:
        raise RuntimeError(f"unreadable logo: {path}")
    img = flatten_logo(decoded)
    ok, buf = cv2.imencode(".jpg", img, [int(cv2.IMWRITE_JPEG_QUALITY), 90])
    if not ok:
        raise RuntimeError(f"jpeg encode failed: {path}")
    vectors = embed_frames_batch_bytes_sync([buf.tobytes()], downscale=True)
    if not vectors or not vectors[0]:
        raise RuntimeError(f"empty embedding for {path}")
    return list(vectors[0])


async def _upsert_startup(session: AsyncSession, entry: CatalogStartup) -> Startup:
    row = (
        await session.execute(select(Startup).where(Startup.slug == entry.slug))
    ).scalar_one_or_none()
    logo = entry.resolve_logo()
    aliases = list(dict.fromkeys([*(entry.aliases or []), entry.name.lower()]))
    if row is None:
        row = Startup(slug=entry.slug, name=entry.name)
        session.add(row)
    row.name = entry.name
    row.aliases = aliases
    row.cohort = entry.cohort
    row.sector = entry.sector
    row.one_liner = entry.one_liner
    row.logo_path = logo
    await session.flush()
    return row


async def _upsert_founders(
    session: AsyncSession,
    startup: Startup,
    entry: CatalogStartup,
    stats: IngestStats,
) -> None:
    existing = {
        f.founder_name.strip().casefold(): f
        for f in (
            await session.execute(
                select(StartupFounder).where(StartupFounder.startup_id == startup.id)
            )
        ).scalars().all()
    }
    primary_keys = {f.name.strip().casefold() for f in entry.founders if f.name.strip()}
    for founder in entry.founders:
        key = founder.name.strip().casefold()
        if not key:
            continue
        row = existing.get(key)
        if row is None:
            row = StartupFounder(
                startup_id=startup.id,
                founder_name=founder.name.strip(),
                is_candidate=False,
            )
            session.add(row)
            stats.founders_upserted += 1
            existing[key] = row
        else:
            row.is_candidate = False
            row.founder_name = founder.name.strip()
    for name in entry.candidate_founders:
        key = name.strip().casefold()
        if not key or key in primary_keys or key in existing:
            continue
        session.add(
            StartupFounder(
                startup_id=startup.id,
                founder_name=name.strip(),
                is_candidate=True,
            )
        )
        stats.founders_upserted += 1
    await session.flush()


async def _link_or_create_cluster(
    session: AsyncSession,
    startup: Startup,
    embedding: list[float],
    source_asset: str,
    stats: IngestStats,
) -> None:
    settings = get_settings()
    threshold = settings.person_match_threshold

    candidate, sim = await _find_best_cluster(session, embedding)
    cluster: FaceCluster | None = None
    similarity: float | None = None
    if candidate is not None and sim >= threshold and candidate.centroid is not None:
        locked_sim = cosine_similarity(embedding, list(candidate.centroid))
        if locked_sim >= threshold:
            cluster = candidate
            similarity = locked_sim

    if cluster is None:
        cluster = FaceCluster(
            status=ClusterStatus.UNKNOWN,
            centroid=embedding,
            member_count=0,
            representative_face_id=None,
            person_id=None,
        )
        session.add(cluster)
        await session.flush()
        stats.clusters_created += 1

    link = (
        await session.execute(
            select(StartupFaceCluster).where(
                StartupFaceCluster.startup_id == startup.id,
                StartupFaceCluster.cluster_id == cluster.id,
            )
        )
    ).scalar_one_or_none()
    if link is None:
        session.add(
            StartupFaceCluster(
                startup_id=startup.id,
                cluster_id=cluster.id,
                source_asset=source_asset,
                similarity=similarity,
            )
        )
        stats.face_links_created += 1
    else:
        if source_asset and not link.source_asset:
            link.source_asset = source_asset
        if similarity is not None and (
            link.similarity is None or similarity > float(link.similarity)
        ):
            link.similarity = similarity
        stats.face_links_existing += 1
    await session.flush()


async def _ingest_founder_photos(
    session: AsyncSession,
    startup: Startup,
    entry: CatalogStartup,
    stats: IngestStats,
) -> None:
    for rel in entry.resolve_founder_photos():
        path = asset_path(rel)
        try:
            image = _load_bgr(path)
            if image is None:
                stats.errors.append(f"{entry.slug}: unreadable founder photo {rel}")
                continue
            faces = await _detect_faces(image)
            if not faces:
                stats.errors.append(f"{entry.slug}: no faces in {rel}")
                continue
            # Prefer largest faces first (group shots).
            ranked = sorted(
                faces,
                key=lambda f: float(getattr(f, "bbox_width", 0) or 0)
                * float(getattr(f, "bbox_height", 0) or 0),
                reverse=True,
            )
            for face in ranked:
                emb = getattr(face, "embedding", None)
                if emb is None:
                    continue
                await _link_or_create_cluster(
                    session,
                    startup,
                    [float(v) for v in emb],
                    source_asset=rel,
                    stats=stats,
                )
        except Exception as exc:  # noqa: BLE001
            logger.exception("founder photo ingest failed slug=%s asset=%s", entry.slug, rel)
            stats.errors.append(f"{entry.slug}: founder photo {rel}: {exc}")


async def _ingest_logos(
    session: AsyncSession,
    startup: Startup,
    entry: CatalogStartup,
    stats: IngestStats,
) -> None:
    logo_rels: list[str] = []
    primary = entry.resolve_logo()
    if primary:
        logo_rels.append(primary)
    for rel in entry.logos:
        if rel and rel not in logo_rels and asset_path(rel).is_file():
            logo_rels.append(rel)
            break

    for rel in logo_rels:
        existing = (
            await session.execute(
                select(StartupLogoRef).where(
                    StartupLogoRef.startup_id == startup.id,
                    StartupLogoRef.asset_path == rel,
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            stats.logo_refs_skipped += 1
            continue
        path = asset_path(rel)
        try:
            vector = await asyncio.to_thread(_embed_logo_bytes, path)
        except Exception as exc:  # noqa: BLE001
            logger.exception("logo embed failed slug=%s asset=%s", entry.slug, rel)
            stats.errors.append(f"{entry.slug}: logo {rel}: {exc}")
            continue
        session.add(
            StartupLogoRef(
                startup_id=startup.id,
                asset_path=rel,
                embedding=vector,
            )
        )
        stats.logo_refs_upserted += 1
    await session.flush()


async def ingest_catalog(
    session: AsyncSession,
    *,
    reload: bool = True,
    embed_logos: bool = True,
    link_faces: bool = True,
    refresh_logos: bool = False,
) -> IngestStats:
    """Upsert catalog rows, link founder faces, embed logos. Never names a Person."""
    stats = IngestStats()
    entries = reload_catalog() if reload else load_catalog()
    if not entries:
        stats.errors.append("catalog empty or missing")
        return stats

    if refresh_logos:
        await session.execute(delete(StartupLogoRef))
        await session.commit()

    for entry in entries:
        # Commit per startup so one bad row cannot roll back the whole catalog.
        try:
            startup = await _upsert_startup(session, entry)
            await _upsert_founders(session, startup, entry, stats)
            if link_faces:
                await _ingest_founder_photos(session, startup, entry, stats)
            if embed_logos:
                await _ingest_logos(session, startup, entry, stats)
            await session.commit()
            stats.startups_upserted += 1
        except Exception as exc:  # noqa: BLE001
            await session.rollback()
            logger.exception("startup ingest failed slug=%s", entry.slug)
            stats.errors.append(f"{entry.slug}: {exc}")

    return stats
