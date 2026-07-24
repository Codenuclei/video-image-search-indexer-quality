from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.session import get_db, get_session_factory
from app.runtime_settings import get_runtime_settings
from app.schemas import SearchMoment, SearchResponse, SearchResultFile
from app.gemini.service import SearchCitation, get_gemini_service
from app.gemini.caption_filter import filter_images_by_caption_llm
from app.gemini.rerank import rerank_image_files
from app.search.moments import search_video_moments
from app.search.images import attach_stored_captions, search_image_files
from app.search.local import (
    expand_visual_query,
    files_for_citation_names,
    files_for_drive_ids,
    filter_by_mime,
    filter_files_by_role_context,
    filter_non_student_solo_in_student_context,
    find_files_by_role_context,
    filter_to_tagged_persons,
    sort_by_person_overlap,
    boost_multi_person_scores,
    dedupe_search_files,
    find_matching_files,
    has_strong_filename_match,
    is_action_query,
    action_match_keywords,
    build_strict_action_pool,
    caption_matches_action,
    caption_contradicts_action,
    finalize_action_search_results,
    is_weak_person_visual,
    merge_action_search_pool,
    merge_person_scene_results,
    resolve_role_matching_file_ids,
    needs_semantic_search,
    needs_strict_relevance_filter,
    resolve_search_context,
    role_context_active,
    text_matches_query,
    SearchRoleContext,
)


def _apply_vector_metadata(
    base_files: list[SearchResultFile],
    vector_files: list[SearchResultFile],
    *,
    default_score_for_tagged: float | None = None,
) -> list[SearchResultFile]:
    """Merge vector scores/captions onto face-tagged or local hits."""
    by_id = {f.drive_file_id: f for f in vector_files}
    merged: list[SearchResultFile] = []
    seen: set[str] = set()

    for vf in vector_files:
        merged.append(vf)
        seen.add(vf.drive_file_id)

    for item in base_files:
        if item.drive_file_id in seen:
            continue
        vec = by_id.get(item.drive_file_id)
        update: dict = {}
        if vec and vec.score is not None:
            update["score"] = vec.score
        if vec and vec.caption:
            update["caption"] = vec.caption
        if "score" not in update and default_score_for_tagged is not None and item.person_names:
            update["score"] = default_score_for_tagged
        merged.append(item.model_copy(update=update) if update else item)
        seen.add(item.drive_file_id)

    return merged


async def _filter_moments_for_context(
    session: AsyncSession,
    moments: list[SearchMoment],
    *,
    effective_persons: list[str],
    role_ctx: SearchRoleContext,
) -> list[SearchMoment]:
    """Apply multi-person and role filters to video moment hits."""
    if len(effective_persons) > 1:
        required = {p.lower() for p in effective_persons}
        moments = [
            m for m in moments
            if required.issubset({n.lower() for n in m.person_names})
        ]
    if role_context_active(role_ctx) and moments:
        valid_ids = set(
            await resolve_role_matching_file_ids(
                session,
                list({m.drive_file_id for m in moments}),
                person_names=effective_persons,
                role_ctx=role_ctx,
            )
        )
        moments = [m for m in moments if m.drive_file_id in valid_ids]
    return moments


router = APIRouter(prefix="/search", tags=["search"])
logger = logging.getLogger(__name__)


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
    source: str | None = None,
    rerank: bool = True,
    captions: bool = False,
    session: AsyncSession = Depends(get_db),
) -> SearchResponse:
    """Return matching indexed Drive files. Set captions=true to fuse caption vectors at search time."""
    query = q.strip()
    if not query:
        raise HTTPException(status_code=400, detail="Query cannot be empty")

    mime_filter = (mime or "all").strip().lower()
    if mime_filter not in ("all", "image", "pdf", "video"):
        raise HTTPException(status_code=400, detail="mime must be all, image, pdf, or video")

    source_filter = (source or "all").strip().lower()
    if source_filter not in ("all", "youtube", "drive"):
        raise HTTPException(status_code=400, detail="source must be all, youtube, or drive")
    if source_filter == "all":
        source_filter = None

    effective_persons, visual_query, role_ctx = await resolve_search_context(session, query, person)

    student_role_action = (
        role_context_active(role_ctx)
        and is_action_query(visual_query or query)
        and not effective_persons
        and not role_ctx.co_occur_roles
        and set(role_ctx.require_all_roles) <= {"student"}
    )
    action_query = (
        is_action_query(visual_query or query)
        and not effective_persons
        and (not role_context_active(role_ctx) or student_role_action)
    )
    person_action = bool(effective_persons and is_action_query(visual_query or query))
    use_captions = captions or action_query or role_context_active(role_ctx) or person_action
    use_rerank = rerank and get_runtime_settings().search_rerank_enabled

    person_focused = bool(
        effective_persons
        and is_weak_person_visual(visual_query, effective_persons)
        and not role_context_active(role_ctx)
    )
    primary_person = effective_persons[0] if len(effective_persons) == 1 else None

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

    # Video-only (Carousel / mime=video): skip image pipelines entirely.
    # Uses Gemini Embedding 2 → Qdrant frame search + transcript/face + VLM rerank.
    if mime_filter == "video":
        moments = await search_video_moments(
            session,
            query=query,
            visual_query=visual_query,
            person_name=primary_person,
            folder_path=folder_path,
            folder_context=folder_description,
            rerank=use_rerank,
            action_query=action_query,
            source=source_filter,
        )
        moments = await _filter_moments_for_context(
            session,
            moments,
            effective_persons=effective_persons,
            role_ctx=role_ctx,
        )
        return SearchResponse(
            query=query,
            answer="",
            files=[],
            citations=[],
            moments=moments,
        )

    vector_text = query if person_focused else (visual_query or query)
    session_factory = get_session_factory()

    async def _load_local_files() -> list[SearchResultFile]:
        async with session_factory() as local_session:
            if action_query and not effective_persons:
                return []
            if effective_persons and not person_focused:
                hits = await find_matching_files(local_session, "", person_names=effective_persons)
            elif role_context_active(role_ctx) and not effective_persons:
                hits = await find_files_by_role_context(local_session, role_ctx)
            else:
                hits = await find_matching_files(
                    local_session,
                    "" if person_focused else visual_query,
                    person_names=effective_persons if person_focused else None,
                )
            if role_context_active(role_ctx):
                hits = await filter_files_by_role_context(
                    local_session,
                    hits,
                    person_names=effective_persons,
                    role_ctx=role_ctx,
                )
            return hits

    async def _load_vector_files() -> list[SearchResultFile]:
        async with session_factory() as vector_session:
            return await search_image_files(
                vector_session,
                vector_text,
                person_names=effective_persons or None,
                folder_path=folder_path,
                use_captions=use_captions,
                action_query=action_query,
            )

    local_files, vector_image_files = await asyncio.gather(
        _load_local_files(),
        _load_vector_files(),
    )
    gemini_files: list = []

    file_search_on = get_runtime_settings().gemini_file_search_search_enabled
    skip_gemini = (
        not file_search_on
        or (
            not effective_persons
            and needs_strict_relevance_filter(query)
            and has_strong_filename_match(query, local_files)
        )
    )

    if needs_semantic_search(visual_query or query, primary_person, len(local_files)) and not skip_gemini:
        try:
            gemini_files = await _gemini_files(
                session,
                visual_query,
                primary_person,
                relevance_query=visual_query or query,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Gemini semantic search failed, falling back to local: %s", exc)

    files = merge_person_scene_results(
        local_files,
        gemini_files,
        person_names=effective_persons,
        visual_query=visual_query,
        original_query=query,
    )
    if effective_persons and person_focused:
        files = _apply_vector_metadata(
            files,
            vector_image_files,
            default_score_for_tagged=0.5,
        )
    elif mime_filter in ("all", "image"):
        if action_query and not effective_persons:
            files = vector_image_files or []
        elif vector_image_files:
            if effective_persons:
                files = _apply_vector_metadata(
                    files,
                    vector_image_files,
                    default_score_for_tagged=0.45,
                )
            elif role_context_active(role_ctx) and local_files:
                files = _apply_vector_metadata(
                    local_files,
                    vector_image_files,
                    default_score_for_tagged=0.45,
                )
            else:
                files = vector_image_files or local_files
    files = filter_by_mime(files, mime_filter)

    # Scope to folder if requested (skip for root "/" — that means all files)
    if folder_path and folder_path.strip() and folder_path.strip() != "/":
        fp = folder_path.strip().rstrip("/")
        files = [f for f in files if f.path.startswith(fp + "/") or f.path == fp]

    if effective_persons:
        files = filter_to_tagged_persons(files, effective_persons)
        if len(effective_persons) > 1:
            files = boost_multi_person_scores(files, effective_persons)
            files = sort_by_person_overlap(files, effective_persons)
        files = await filter_non_student_solo_in_student_context(
            session,
            files,
            person_names=effective_persons,
            role_ctx=role_ctx,
        )

    if role_context_active(role_ctx):
        files = await filter_files_by_role_context(
            session,
            files,
            person_names=effective_persons,
            role_ctx=role_ctx,
        )

    if needs_strict_relevance_filter(query) and not effective_persons and not role_context_active(role_ctx):
        files = [f for f in files if text_matches_query(f.name, f.path, query=query)]

    if role_context_active(role_ctx) and not (action_query and not effective_persons):
        files = [
            item.model_copy(update={"score": item.score or 0.55})
            if item.mime_type.startswith("image/") and item.score is None
            else item
            for item in files
        ]

    # Images without a vector match score are unreliable — only return scored hits.
    files = [
        f for f in files
        if f.score is not None or not f.mime_type.startswith("image/")
    ]

    image_files = [f for f in files if f.mime_type.startswith("image/")]
    other_files = [f for f in files if not f.mime_type.startswith("image/")]

    # Captions must exist before keyword match, rerank, and LLM caption filter.
    if image_files:
        image_files = await attach_stored_captions(image_files)

    keyword_matched: list[SearchResultFile] = []
    if (action_query or person_action) and image_files and use_captions:
        keywords = action_match_keywords(query)
        keyword_matched = [
            f for f in image_files
            if f.caption
            and caption_matches_action(f.caption, keywords)
            and not caption_contradicts_action(f.caption, query)
        ]
        if action_query and not person_action:
            image_files = build_strict_action_pool(
                image_files,
                keyword_matched,
                keywords,
                query,
            )
        elif keyword_matched:
            image_files = merge_action_search_pool(image_files, keyword_matched)

    if use_rerank and image_files and not get_settings().search_caption_filter_enabled:
        pre_rerank = list(image_files)
        try:
            reranked = await rerank_image_files(
                query,
                image_files,
                person_name=primary_person,
                folder_context=folder_description,
                strict_action=action_query,
            )
            if reranked:
                image_files = reranked
            elif action_query and keyword_matched:
                image_files = keyword_matched[:12]
            else:
                image_files = pre_rerank[:12]
        except Exception as exc:  # noqa: BLE001
            logger.warning("Image re-rank failed, returning unfiltered results: %s", exc)

    files = dedupe_search_files(image_files + other_files)

    moments = await search_video_moments(
        session,
        query=query,
        visual_query=visual_query,
        person_name=primary_person,
        folder_path=folder_path,
        folder_context=folder_description,
        rerank=use_rerank,
        action_query=action_query,
        source=source_filter,
    )
    moments = await _filter_moments_for_context(
        session,
        moments,
        effective_persons=effective_persons,
        role_ctx=role_ctx,
    )
    if mime_filter in ("image", "pdf"):
        moments = []

    # Append-only: caption-text LLM filter (only when caption matching is active).
    if use_rerank and use_captions and mime_filter in ("all", "image"):
        image_hits = [f for f in files if f.mime_type.startswith("image/")]
        other_hits = [f for f in files if not f.mime_type.startswith("image/")]
        if image_hits and role_ctx.student_context:
            from app.search.local import drive_file_ids_with_student_captions

            student_cap_ids = set(
                await drive_file_ids_with_student_captions(
                    session,
                    [f.drive_file_id for f in image_hits],
                )
            )
            if student_cap_ids:
                image_hits = [f for f in image_hits if f.drive_file_id in student_cap_ids]
        if image_hits:
            pre_filter_hits = list(image_hits)
            try:
                filtered_images = await filter_images_by_caption_llm(
                    query,
                    image_hits,
                    visual_query=visual_query or query,
                    person_names=effective_persons,
                    role_ctx=role_ctx,
                    folder_context=folder_description,
                    strict_action=action_query or person_action,
                )
                if action_query or person_action:
                    if action_query and not person_action:
                        keywords = action_match_keywords(query)
                        filtered_images = [
                            f for f in filtered_images
                            if f.caption
                            and caption_matches_action(f.caption, keywords)
                            and not caption_contradicts_action(f.caption, query)
                        ]
                    filtered_images = finalize_action_search_results(
                        filtered_images,
                        keyword_matched,
                        max_results=12 if action_query and not person_action else 20,
                    )
                if not filtered_images and pre_filter_hits:
                    if keyword_matched:
                        cap = 12 if action_query and not person_action else 20
                        filtered_images = keyword_matched[:cap]
                    elif not (person_action or action_query):
                        filtered_images = sorted(
                            pre_filter_hits,
                            key=lambda f: (-(f.score or 0.0), f.name.lower()),
                        )[:30]
                        logger.warning(
                            "Caption filter returned 0 for %r — fallback %d hit(s)",
                            query,
                            len(filtered_images),
                        )
                files = dedupe_search_files(filtered_images + other_hits)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Caption LLM filter failed, keeping unfiltered results: %s", exc)

    return SearchResponse(
        query=query,
        answer="",
        files=files,
        citations=[],
        moments=moments,
    )
