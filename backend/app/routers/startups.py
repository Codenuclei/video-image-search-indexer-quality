"""Startup catalog browse + ingest admin endpoints."""

from __future__ import annotations

import mimetypes
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    FaceCluster,
    Person,
    Startup,
    StartupFaceCluster,
    StartupFounder,
    StartupLogoRef,
)
from app.db.session import get_db, get_session_factory
from app.routers.app_auth import require_app_admin
from app.startups.catalog import load_catalog
from app.startups.ingest import ingest_catalog
from app.startups.paths import asset_path as resolve_asset
from app.startups.paths import catalog_path, startups_data_dir

router = APIRouter(prefix="/startups", tags=["startups"])


class StartupFounderOut(BaseModel):
    id: int
    founder_name: str
    person_id: int | None = None
    person_name: str | None = None
    is_candidate: bool = False


class StartupClusterOut(BaseModel):
    cluster_id: int
    status: str
    member_count: int
    source_asset: str | None = None
    similarity: float | None = None
    person_id: int | None = None
    person_name: str | None = None


class StartupOut(BaseModel):
    id: int
    slug: str
    name: str
    aliases: list[str] = Field(default_factory=list)
    cohort: str | None = None
    sector: str | None = None
    one_liner: str | None = None
    logo_path: str | None = None
    founders: list[StartupFounderOut] = Field(default_factory=list)
    face_clusters: list[StartupClusterOut] = Field(default_factory=list)
    logo_ref_count: int = 0


class StartupListResponse(BaseModel):
    items: list[StartupOut]
    total: int
    catalog_present: bool
    catalog_count: int


class IngestResponse(BaseModel):
    ok: bool
    stats: dict


def _aliases(raw) -> list[str]:
    if isinstance(raw, list):
        return [str(a) for a in raw if str(a).strip()]
    return []


def _safe_asset(rel: str) -> Path | None:
    path = resolve_asset(rel)
    root = startups_data_dir().resolve()
    try:
        resolved = path.resolve()
        resolved.relative_to(root)
    except Exception:  # noqa: BLE001
        return None
    if not resolved.is_file():
        return None
    return resolved


async def _serialize_startup(session: AsyncSession, startup: Startup) -> StartupOut:
    founders = list(
        (
            await session.execute(
                select(StartupFounder).where(StartupFounder.startup_id == startup.id)
            )
        ).scalars().all()
    )
    person_ids = [f.person_id for f in founders if f.person_id is not None]
    person_names: dict[int, str] = {}
    if person_ids:
        person_names = {
            int(pid): name
            for pid, name in (
                await session.execute(
                    select(Person.id, Person.name).where(Person.id.in_(person_ids))
                )
            ).all()
        }

    links = list(
        (
            await session.execute(
                select(StartupFaceCluster, FaceCluster, Person)
                .join(FaceCluster, FaceCluster.id == StartupFaceCluster.cluster_id)
                .outerjoin(Person, Person.id == FaceCluster.person_id)
                .where(StartupFaceCluster.startup_id == startup.id)
            )
        ).all()
    )
    logo_count = (
        await session.execute(
            select(func.count())
            .select_from(StartupLogoRef)
            .where(StartupLogoRef.startup_id == startup.id)
        )
    ).scalar_one()

    return StartupOut(
        id=startup.id,
        slug=startup.slug,
        name=startup.name,
        aliases=_aliases(startup.aliases),
        cohort=startup.cohort,
        sector=startup.sector,
        one_liner=startup.one_liner,
        logo_path=startup.logo_path,
        founders=[
            StartupFounderOut(
                id=f.id,
                founder_name=f.founder_name,
                person_id=f.person_id,
                person_name=person_names.get(f.person_id) if f.person_id else None,
                is_candidate=bool(f.is_candidate),
            )
            for f in founders
        ],
        face_clusters=[
            StartupClusterOut(
                cluster_id=cluster.id,
                status=cluster.status.value,
                member_count=cluster.member_count,
                source_asset=link.source_asset,
                similarity=link.similarity,
                person_id=person.id if person else None,
                person_name=person.name if person else None,
            )
            for link, cluster, person in links
        ],
        logo_ref_count=int(logo_count or 0),
    )


@router.get("", response_model=StartupListResponse)
async def list_startups(session: AsyncSession = Depends(get_db)) -> StartupListResponse:
    rows = list(
        (await session.execute(select(Startup).order_by(Startup.name))).scalars().all()
    )
    items = [await _serialize_startup(session, row) for row in rows]
    catalog = load_catalog()
    return StartupListResponse(
        items=items,
        total=len(items),
        catalog_present=catalog_path().is_file(),
        catalog_count=len(catalog),
    )


@router.get("/by-slug/{slug}", response_model=StartupOut)
async def get_startup(slug: str, session: AsyncSession = Depends(get_db)) -> StartupOut:
    row = (
        await session.execute(select(Startup).where(Startup.slug == slug))
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="Startup not found")
    return await _serialize_startup(session, row)


@router.get("/asset/{file_path:path}")
async def startup_asset(file_path: str):
    """Serve a catalog asset (logo / founder photo) for the UI."""
    rel = file_path if file_path.startswith("assets/") else f"assets/{file_path}"
    path = _safe_asset(rel)
    if path is None:
        raise HTTPException(status_code=404, detail="Asset not found")
    mime, _ = mimetypes.guess_type(str(path))
    return FileResponse(path, media_type=mime or "application/octet-stream")


@router.post("/ingest", response_model=IngestResponse)
async def ingest_startups(
    background: BackgroundTasks,
    _admin: str = Depends(require_app_admin),
    wait: bool = False,
) -> IngestResponse:
    """Admin: upsert catalog, link founder faces, embed logos."""
    if wait:
        factory = get_session_factory()
        async with factory() as session:
            stats = await ingest_catalog(session)
        return IngestResponse(ok=len(stats.errors) == 0, stats=stats.as_dict())

    async def _run() -> None:
        factory = get_session_factory()
        async with factory() as session:
            await ingest_catalog(session)

    background.add_task(_run)
    return IngestResponse(ok=True, stats={"queued": True})
