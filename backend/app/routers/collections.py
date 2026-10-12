"""
app/routers/collections.py
==========================
Shareable collections of Drive files, owned per signed-in user email.

Identity is the sealed app session token (Authorization: Bearer).
"""
from __future__ import annotations

import secrets
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.auth_session import current_user_email
from app.db.models import Collection, CollectionItem, CollectionView
from app.db.session import get_db

router = APIRouter(prefix="/collections", tags=["collections"])

MAX_NAME_LEN = 120
MAX_ITEMS_PER_REQUEST = 100
MAX_QUERY_LEN = 500


# ── Schemas ──────────────────────────────────────────────────────────────────

class CollectionCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=MAX_NAME_LEN)


class CollectionRename(BaseModel):
    name: str = Field(..., min_length=1, max_length=MAX_NAME_LEN)


class CollectionItemIn(BaseModel):
    drive_file_id: str = Field(..., min_length=1, max_length=128)
    name: str = Field(..., min_length=1, max_length=512)
    mime_type: str = Field(..., min_length=1, max_length=128)
    query: str | None = Field(default=None, max_length=MAX_QUERY_LEN)


class CollectionItemsIn(BaseModel):
    items: list[CollectionItemIn] = Field(..., min_length=1, max_length=MAX_ITEMS_PER_REQUEST)


class CollectionItemOut(BaseModel):
    id: int
    drive_file_id: str
    name: str
    mime_type: str
    query: str | None
    added_at: datetime | None

    model_config = {"from_attributes": True}


class CollectionSummaryOut(BaseModel):
    id: str
    name: str
    owner_email: str
    created_at: datetime | None
    item_count: int
    cover_drive_file_ids: list[str]
    contains_file: bool = False


class SharedCollectionSummaryOut(CollectionSummaryOut):
    last_seen_at: datetime | None


class CollectionListOut(BaseModel):
    mine: list[CollectionSummaryOut]
    shared: list[SharedCollectionSummaryOut]


class CollectionDetailOut(BaseModel):
    id: str
    name: str
    owner_email: str
    created_at: datetime | None
    is_owner: bool
    items: list[CollectionItemOut]


# ── Helpers ──────────────────────────────────────────────────────────────────

def normalize_name(name: str) -> str:
    cleaned = name.strip()
    if not cleaned:
        raise HTTPException(status_code=422, detail="Name is required")
    if len(cleaned) > MAX_NAME_LEN:
        cleaned = cleaned[:MAX_NAME_LEN]
    return cleaned


def new_collection_id() -> str:
    return secrets.token_urlsafe(9)


def is_owner(collection: Collection, email: str | None) -> bool:
    if not email:
        return False
    return collection.owner_email.lower() == email.lower()


def require_owner(collection: Collection, email: str) -> None:
    if not is_owner(collection, email):
        raise HTTPException(status_code=403, detail="Not the collection owner")


async def get_collection_or_404(session: AsyncSession, collection_id: str) -> Collection:
    row = (
        await session.execute(select(Collection).where(Collection.id == collection_id))
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="Collection not found")
    return row


async def get_collection_with_items_or_404(
    session: AsyncSession, collection_id: str
) -> Collection:
    row = (
        await session.execute(
            select(Collection)
            .where(Collection.id == collection_id)
            .options(selectinload(Collection.items))
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="Collection not found")
    return row


def _item_sort_key(item: CollectionItem) -> float:
    if item.added_at is None:
        return 0.0
    return item.added_at.timestamp()


def _summary(row: Collection, *, contains_file: bool = False) -> CollectionSummaryOut:
    ordered = sorted(row.items, key=_item_sort_key, reverse=True)
    return CollectionSummaryOut(
        id=row.id,
        name=row.name,
        owner_email=row.owner_email,
        created_at=row.created_at,
        item_count=len(row.items),
        cover_drive_file_ids=[item.drive_file_id for item in ordered[:4]],
        contains_file=contains_file,
    )


def collection_detail(collection: Collection, email: str | None) -> CollectionDetailOut:
    items = sorted(collection.items, key=_item_sort_key, reverse=True)
    return CollectionDetailOut(
        id=collection.id,
        name=collection.name,
        owner_email=collection.owner_email,
        created_at=collection.created_at,
        is_owner=is_owner(collection, email),
        items=[CollectionItemOut.model_validate(item) for item in items],
    )


async def record_shared_view(session: AsyncSession, collection_id: str, viewer_email: str) -> None:
    """Upsert a CollectionView for a non-owner opener (race-safe)."""
    now = datetime.now(timezone.utc)
    stmt = (
        insert(CollectionView)
        .values(
            collection_id=collection_id,
            viewer_email=viewer_email,
            first_seen_at=now,
            last_seen_at=now,
        )
        .on_conflict_do_update(
            constraint="uq_collection_view_viewer",
            set_={"last_seen_at": now},
        )
    )
    await session.execute(stmt)


# ── Routes ───────────────────────────────────────────────────────────────────

@router.get("", response_model=CollectionListOut)
async def list_collections(
    session: AsyncSession = Depends(get_db),
    email: str = Depends(current_user_email),
    file_id: str | None = Query(default=None, max_length=128),
):
    check_file_id = (file_id or "").strip() or None

    mine_rows = (
        await session.execute(
            select(Collection)
            .where(func.lower(Collection.owner_email) == email)
            .order_by(Collection.created_at.desc())
            .options(selectinload(Collection.items))
        )
    ).scalars().all()

    shared_rows = (
        await session.execute(
            select(Collection, CollectionView)
            .join(CollectionView, CollectionView.collection_id == Collection.id)
            .where(func.lower(CollectionView.viewer_email) == email)
            .where(func.lower(Collection.owner_email) != email)
            .order_by(CollectionView.last_seen_at.desc())
            .options(selectinload(Collection.items))
        )
    ).all()

    mine_out: list[CollectionSummaryOut] = []
    for row in mine_rows:
        contains = (
            any(item.drive_file_id == check_file_id for item in row.items)
            if check_file_id
            else False
        )
        mine_out.append(_summary(row, contains_file=contains))

    shared_out: list[SharedCollectionSummaryOut] = []
    for collection, view in shared_rows:
        base = _summary(collection, contains_file=False)
        shared_out.append(
            SharedCollectionSummaryOut(
                **base.model_dump(),
                last_seen_at=view.last_seen_at,
            )
        )

    return CollectionListOut(mine=mine_out, shared=shared_out)


@router.post("", response_model=CollectionDetailOut, status_code=201)
async def create_collection(
    body: CollectionCreate,
    session: AsyncSession = Depends(get_db),
    email: str = Depends(current_user_email),
):
    name = normalize_name(body.name)
    row = Collection(id=new_collection_id(), owner_email=email, name=name)
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return CollectionDetailOut(
        id=row.id,
        name=row.name,
        owner_email=row.owner_email,
        created_at=row.created_at,
        is_owner=True,
        items=[],
    )


@router.get("/{collection_id}", response_model=CollectionDetailOut)
async def get_collection(
    collection_id: str,
    session: AsyncSession = Depends(get_db),
    email: str = Depends(current_user_email),
):
    row = await get_collection_with_items_or_404(session, collection_id)
    if not is_owner(row, email):
        await record_shared_view(session, row.id, email)
        await session.commit()
    return collection_detail(row, email)


@router.patch("/{collection_id}", response_model=CollectionDetailOut)
async def rename_collection(
    collection_id: str,
    body: CollectionRename,
    session: AsyncSession = Depends(get_db),
    email: str = Depends(current_user_email),
):
    row = await get_collection_with_items_or_404(session, collection_id)
    require_owner(row, email)
    row.name = normalize_name(body.name)
    await session.commit()
    row = await get_collection_with_items_or_404(session, collection_id)
    return collection_detail(row, email)


@router.delete("/{collection_id}", status_code=204)
async def delete_collection(
    collection_id: str,
    session: AsyncSession = Depends(get_db),
    email: str = Depends(current_user_email),
):
    row = await get_collection_or_404(session, collection_id)
    require_owner(row, email)
    await session.delete(row)
    await session.commit()


@router.delete("/{collection_id}/view", status_code=204)
async def remove_collection_view(
    collection_id: str,
    session: AsyncSession = Depends(get_db),
    email: str = Depends(current_user_email),
):
    """Remove this collection from the caller's Shared with me list only."""
    view = (
        await session.execute(
            select(CollectionView).where(
                CollectionView.collection_id == collection_id,
                func.lower(CollectionView.viewer_email) == email,
            )
        )
    ).scalar_one_or_none()
    if view:
        await session.delete(view)
        await session.commit()


@router.post("/{collection_id}/items", response_model=CollectionDetailOut)
async def add_collection_items(
    collection_id: str,
    body: CollectionItemsIn,
    session: AsyncSession = Depends(get_db),
    email: str = Depends(current_user_email),
):
    row = await get_collection_with_items_or_404(session, collection_id)
    require_owner(row, email)

    existing_ids = {item.drive_file_id for item in row.items}
    for incoming in body.items:
        drive_file_id = incoming.drive_file_id.strip()
        if not drive_file_id or drive_file_id in existing_ids:
            continue
        query_text = (incoming.query.strip() if incoming.query else None) or None
        if query_text and len(query_text) > MAX_QUERY_LEN:
            query_text = query_text[:MAX_QUERY_LEN]
        stmt = (
            insert(CollectionItem)
            .values(
                collection_id=row.id,
                drive_file_id=drive_file_id,
                name=incoming.name.strip() or drive_file_id,
                mime_type=incoming.mime_type.strip() or "application/octet-stream",
                query=query_text,
            )
            .on_conflict_do_nothing(constraint="uq_collection_item_file")
        )
        await session.execute(stmt)
        existing_ids.add(drive_file_id)

    await session.commit()
    row = await get_collection_with_items_or_404(session, collection_id)
    return collection_detail(row, email)


@router.delete("/{collection_id}/items/{drive_file_id}", response_model=CollectionDetailOut)
async def remove_collection_item(
    collection_id: str,
    drive_file_id: str,
    session: AsyncSession = Depends(get_db),
    email: str = Depends(current_user_email),
):
    row = await get_collection_with_items_or_404(session, collection_id)
    require_owner(row, email)

    item = next((i for i in row.items if i.drive_file_id == drive_file_id), None)
    if item is None:
        raise HTTPException(status_code=404, detail="Item not found")
    await session.delete(item)
    await session.commit()
    row = await get_collection_with_items_or_404(session, collection_id)
    return collection_detail(row, email)
