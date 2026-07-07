from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.schemas import SearchResponse
from app.gemini.service import SearchCitation, get_gemini_service
from app.search.moments import search_video_moments
from app.search.images import search_image_files
from app.search.local import (
    expand_visual_query,
    files_for_citation_names,
    files_for_drive_ids,
    filter_by_mime,
    filter_to_tagged_person,
    find_matching_files,
    has_strong_filename_match,
    merge_person_scene_results,
    needs_semantic_search,
    needs_strict_relevance_filter,
    resolve_search_context,
    text_matches_query,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/search", tags=["search"])


def _citation_is_relevant(citation: SearchCitation, query: str, person: str | None) -> bool:
    if person and person.strip():
        return True
    if not needs_strict_relevance_filter(query):
        return True
    return text_matches_query(
        citation.file_name,
        citation.drive_path,
        citation.source,
        query=query,
    )


def _gemini_query(visual_query: str, person: str | None) -> str:
    if person and visual_query.strip().lower() == person.strip().lower():
        return "photos of this person"
    return visual_query or "photos"


async def _gemini_files(
    session: AsyncSession,
    visual_query: str,
    person: str | None,
    *,
    relevance_query: str,
) -> list:
    gemini = get_gemini_service()
    merged: list = []
    seen_ids: set[str] = set()
    gemini_query = _gemini_query(visual_query, person)

    for variant in expand_visual_query(gemini_query):
        result = await asyncio.to_thread(gemini.search, variant, person)
        relevant = [c for c in result.citations if _citation_is_relevant(c, relevance_query, person)]
        citation_ids = [c.drive_file_id for c in relevant if c.drive_file_id]
        variant_files = await files_for_drive_ids(session, citation_ids)
        if not variant_files:
            names = [c.file_name for c in relevant if c.file_name]
            variant_files = await files_for_citation_names(session, names)
        for item in variant_files:
            if item.drive_file_id in seen_ids:
                continue
            seen_ids.add(item.drive_file_id)
            merged.append(item)

    return merged


@router.get("", response_model=SearchResponse)
async def search(
    q: str,
    person: str | None = None,
    mime: str | None = None,
    folder_path: str | None = None,
    rerank: bool = True,
    session: AsyncSession = Depends(get_db),
) -> SearchResponse:
    """Return matching indexed Drive files. Images/docs use Gemini; video moments use Gemini Embedding 2 + re-ranking."""
    query = q.strip()
    if not query:
        raise HTTPException(status_code=400, detail="Query cannot be empty")

    mime_filter = (mime or "all").strip().lower()
    if mime_filter not in ("all", "image", "pdf", "video"):
        raise HTTPException(status_code=400, detail="mime must be all, image, pdf, or video")

    effective_person, visual_query = await resolve_search_context(session, query, person)

    # Look up folder context description for re-ranker and scoping
    folder_description: str | None = None
    if folder_path and folder_path.strip():
        from sqlalchemy import select as sa_select
        from app.db.models import FolderContext
        fc = (
            await session.execute(
                sa_select(FolderContext).where(FolderContext.folder_path == folder_path.strip())
            )
        ).scalar_one_or_none()
        if fc:
            folder_description = fc.description

    local_files = await find_matching_files(session, visual_query, effective_person)
    vector_image_files = await search_image_files(
        session,
        visual_query or query,
        person_name=effective_person,
        folder_path=folder_path,
    )
    gemini_files: list = []

    skip_gemini = (
        not effective_person
        and needs_strict_relevance_filter(query)
        and has_strong_filename_match(query, local_files)
    )

    if needs_semantic_search(visual_query or query, effective_person, len(local_files)) and not skip_gemini:
        try:
            gemini_files = await _gemini_files(
                session,
                visual_query,
                effective_person,
                relevance_query=visual_query or query,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Gemini semantic search failed, falling back to local: %s", exc)

    files = merge_person_scene_results(
        local_files,
        gemini_files,
        person_name=effective_person,
        visual_query=visual_query,
        original_query=query,
    )
    # Prepend vector image hits (higher visual precision than filename / File Search alone)
    if vector_image_files and mime_filter in ("all", "image"):
        seen = {f.drive_file_id for f in files}
        merged = [f for f in vector_image_files if f.drive_file_id not in seen]
        files = merged + files
    files = filter_by_mime(files, mime_filter)

    # Scope to folder if requested (skip for root "/" — that means all files)
    if folder_path and folder_path.strip() and folder_path.strip() != "/":
        fp = folder_path.strip().rstrip("/")
        files = [f for f in files if f.path.startswith(fp + "/") or f.path == fp]

    if effective_person:
        files = filter_to_tagged_person(files, effective_person)

    if needs_strict_relevance_filter(query) and not effective_person:
        files = [f for f in files if text_matches_query(f.name, f.path, query=query)]

    moments = await search_video_moments(
        session,
        query=query,
        visual_query=visual_query,
        person_name=effective_person,
        folder_path=folder_path,
        folder_context=folder_description,
        rerank=rerank,
    )
    if mime_filter == "video":
        files = []
    elif mime_filter in ("image", "pdf"):
        moments = []

    return SearchResponse(
        query=query,
        answer="",
        files=files,
        citations=[],
        moments=moments,
    )
