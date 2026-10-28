"""Student + object queries ("students receiving cheques") must not collapse to classrooms."""
from __future__ import annotations

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, patch

import pytest

from app.schemas import SearchResultFile
from app.search.local import SearchRoleContext


def _image(fid: str, caption: str, score: float | None) -> SearchResultFile:
    return SearchResultFile(
        drive_file_id=fid,
        name=f"{fid}.jpg",
        path=f"/Events/{fid}.jpg",
        mime_type="image/jpeg",
        score=score,
        caption=caption,
    )


CHEQUE = _image("cheque", "Three people on stage holding a large ceremonial cheque.", 0.44)
BLACK = _image("black", "The image is completely black.", 0.41)


async def _run(*, caption_filter_result: list[SearchResultFile] | Exception):
    from app.routers import search as search_router

    @asynccontextmanager
    async def _session():
        yield AsyncMock()

    role_ctx = SearchRoleContext(require_all_roles=("student",), student_context=True)
    caption_filter = (
        AsyncMock(side_effect=caption_filter_result)
        if isinstance(caption_filter_result, Exception)
        else AsyncMock(return_value=caption_filter_result)
    )
    with (
        patch.object(search_router, "refresh_runtime_settings_from_db", AsyncMock()),
        patch.object(
            search_router,
            "resolve_search_context",
            AsyncMock(return_value=([], "receiving cheques", role_ctx)),
        ),
        patch.object(search_router, "get_session_factory", return_value=_session),
        patch.object(
            search_router, "search_image_files", AsyncMock(return_value=[CHEQUE, BLACK])
        ) as vector_search,
        patch.object(
            search_router,
            "filter_files_by_role_context",
            AsyncMock(side_effect=lambda _s, files, **_k: files),
        ),
        patch.object(
            search_router, "attach_stored_captions", AsyncMock(side_effect=lambda files: files)
        ),
        patch.object(search_router, "search_video_moments", AsyncMock(return_value=[])),
        patch.object(
            search_router, "rerank_image_files", AsyncMock(side_effect=lambda _q, files, **_k: files)
        ),
        patch.object(search_router, "filter_images_by_caption_llm", caption_filter),
    ):
        response = await search_router._run_search(
            session=AsyncMock(),
            query="students receiving cheques",
            person=None,
            mime_filter="image",
            folder_path=None,
            source_filter=None,
            rerank=True,
            captions=True,
            checkpoint=AsyncMock(),
            skip_cache=True,
        )
    return response, vector_search


@pytest.mark.asyncio
async def test_student_object_query_returns_vector_hits_without_placeholder_scores():
    response, vector_search = await _run(caption_filter_result=[CHEQUE])

    assert [f.drive_file_id for f in response.files] == ["cheque"]
    assert response.files[0].score == 0.44
    assert vector_search.await_args.args[1] == "receiving cheques"


@pytest.mark.asyncio
async def test_caption_filter_rejecting_all_does_not_fall_back_to_full_pool():
    response, _ = await _run(caption_filter_result=[])

    assert response.files == []


@pytest.mark.asyncio
async def test_caption_filter_unavailable_keeps_unfiltered_results():
    from app.gemini.caption_filter import CaptionFilterUnavailable

    response, _ = await _run(caption_filter_result=CaptionFilterUnavailable("down"))

    assert {f.drive_file_id for f in response.files} == {"cheque", "black"}
