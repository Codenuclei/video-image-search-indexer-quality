from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest

from app.gemini.caption_filter import filter_images_by_caption_llm
from app.gemini.query_expand import expand_queries_sync
from app.qdrant.image_captions import (
    caption_matches_query_text,
    is_valid_caption,
    search_caption_keywords_sync,
    search_captions_sync,
)
from app.schemas import SearchResultFile
from app.search.images import search_image_files
from app.search.local import is_action_query, parse_role_context


def _file(index: int, *, caption: str | None = None) -> SearchResultFile:
    return SearchResultFile(
        drive_file_id=f"file-{index}",
        name=f"image-{index}.jpg",
        path=f"/Graduation/image-{index}.jpg",
        mime_type="image/jpeg",
        score=0.6,
        caption=caption,
    )


def test_gradutes_expands_to_graduation_concepts():
    expand_queries_sync.cache_clear()
    settings = SimpleNamespace(gemini_api_key="")
    with patch("app.config.get_settings", return_value=settings):
        variants = expand_queries_sync("gradutes")
    expand_queries_sync.cache_clear()

    normalized = " ".join(variants).lower()
    assert "graduates" in normalized
    assert "graduation cap" in normalized
    assert "black and yellow" in normalized
    assert "convocation" in normalized


def test_graduates_is_broad_but_explicit_students_remain_strict():
    graduate_text, graduate_ctx = parse_role_context("graduates")
    assert graduate_text == "graduates"
    assert graduate_ctx.student_context is False
    assert graduate_ctx.co_occur_roles == ()
    assert graduate_ctx.require_all_roles == ()

    student_text, student_ctx = parse_role_context("students graduating")
    assert "graduating" in student_text
    assert student_ctx.student_context is True
    assert student_ctx.require_all_roles == ("student",)


@pytest.mark.parametrize(
    "query",
    [
        "rowing",
        "people exercising",
        "workout",
        "lifting weights",
        "pushing a sled",
        "throwing a medicine ball",
    ],
)
def test_fitness_queries_are_actions(query: str):
    assert is_action_query(query)


def test_blank_black_and_generic_captions_are_invalid():
    assert not is_valid_caption("A completely black image.", min_words=4)
    assert not is_valid_caption("A blank image with no visible content.", min_words=4)
    assert not is_valid_caption("An image of some people.", min_words=4)
    assert is_valid_caption(
        "Graduates in black gowns and yellow stoles throw their caps during convocation.",
        min_words=4,
    )


@pytest.mark.parametrize(
    "caption",
    [
        "A group of graduates pose together outdoors.",
        "People throw graduation caps during a ceremony.",
        "Students wear academic gowns on a stage.",
        "A crowd attends a convocation ceremony.",
        "People in black and yellow gowns wear yellow stoles.",
    ],
)
def test_gradutes_lexical_path_keeps_related_captions(caption: str):
    assert caption_matches_query_text(caption, "gradutes")


def test_unrelated_caption_does_not_match_gradutes():
    assert not caption_matches_query_text(
        "A speaker presents quarterly financial results in a meeting room.",
        "gradutes",
    )


def test_caption_vector_search_reads_every_threshold_page():
    points = [
        SimpleNamespace(
            payload={
                "drive_file_id": f"file-{i}",
                "caption": f"Graduate {i} wearing a cap and academic gown.",
            },
            score=0.6 - i / 100,
        )
        for i in range(3)
    ]
    client = SimpleNamespace()
    client.query_points = Mock(
        side_effect=[
            SimpleNamespace(points=points[:2]),
            SimpleNamespace(points=points[2:]),
        ]
    )
    settings = SimpleNamespace(
        qdrant_image_captions_collection="captions",
        image_caption_min_words=4,
    )

    with (
        patch("app.qdrant.image_captions._client", return_value=client),
        patch("app.config.get_settings", return_value=settings),
    ):
        hits = search_captions_sync(
            [0.1, 0.2],
            limit=None,
            min_score=0.32,
            page_size=2,
        )

    assert [hit["drive_file_id"] for hit in hits] == [
        "file-0",
        "file-1",
        "file-2",
    ]
    assert [call.kwargs["offset"] for call in client.query_points.call_args_list] == [0, 2]


def test_gradutes_keyword_scan_reads_all_pages_and_keeps_every_related_caption():
    captions = [
        "A group of graduates pose together outdoors.",
        "People throw graduation caps during a ceremony.",
        "Students wear academic gowns on a stage.",
        "A crowd attends a convocation ceremony.",
        "People in black and yellow gowns wear yellow stoles.",
    ]
    points = [
        SimpleNamespace(
            payload={"drive_file_id": f"file-{i}", "caption": caption}
        )
        for i, caption in enumerate(captions)
    ]
    client = SimpleNamespace(
        scroll=Mock(
            side_effect=[
                (points[:2], 2),
                (points[2:4], 4),
                (points[4:], None),
            ]
        )
    )
    settings = SimpleNamespace(
        qdrant_image_captions_collection="captions",
        image_caption_min_words=4,
    )

    with (
        patch("app.qdrant.image_captions._client", return_value=client),
        patch("app.config.get_settings", return_value=settings),
    ):
        hits = search_caption_keywords_sync("gradutes", page_size=2)

    assert [hit["drive_file_id"] for hit in hits] == [
        "file-0",
        "file-1",
        "file-2",
        "file-3",
        "file-4",
    ]


@pytest.mark.asyncio
async def test_image_retrieval_does_not_truncate_relevant_candidates():
    count = 45
    settings = SimpleNamespace(
        gemini_api_key="test",
        search_query_expansion=False,
        image_caption_enabled=True,
        gemini_image_result_limit=30,
        gemini_image_min_score=0.25,
        image_visual_weight=0.4,
        image_caption_weight=0.6,
        image_caption_min_score=0.32,
        image_visual_strong_score=0.5,
        search_variant_max_parallel=1,
        cpu_thread_pool_size=1,
    )
    caption_hits = [
        {
            "drive_file_id": f"file-{i}",
            "score": 0.7 - i / 1000,
            "caption": f"Graduate {i} wearing a graduation cap and gown.",
        }
        for i in range(count)
    ]
    drive_files = {
        f"file-{i}": SimpleNamespace(
            id=f"file-{i}",
            name=f"image-{i}.jpg",
            index_name=None,
            path=f"/Graduation/image-{i}.jpg",
            mime_type="image/jpeg",
        )
        for i in range(count)
    }
    session = AsyncMock()
    session.execute.return_value = SimpleNamespace(
        scalars=lambda: SimpleNamespace(all=lambda: list(drive_files.values()))
    )

    with (
        patch("app.search.images.get_settings", return_value=settings),
        patch("app.search.images.embed_text_sync", return_value=[0.1, 0.2]),
        patch("app.search.images.search_images_sync", return_value=[]),
        patch("app.search.images.search_captions_sync", return_value=caption_hits),
        patch("app.search.images.search_caption_keywords_sync", return_value=[]),
        patch(
            "app.search.images.person_names_for_drive_files",
            new=AsyncMock(return_value={}),
        ),
    ):
        results = await search_image_files(
            session,
            "graduates",
            use_captions=True,
        )

    assert len(results) == count
    assert [item.drive_file_id for item in results] == [
        f"file-{i}" for i in range(count)
    ]


@pytest.mark.asyncio
async def test_broad_caption_rerank_preserves_rejected_candidates_in_lower_order():
    first = _file(1, caption="Graduates wearing black gowns and yellow stoles.")
    second = _file(2, caption="People throwing graduation caps during convocation.")
    settings = SimpleNamespace(
        gemini_api_key="test",
        search_caption_filter_enabled=True,
        search_caption_filter_pool_size=120,
        search_caption_filter_batch_size=25,
        search_llm_batch_parallel=1,
        cpu_thread_pool_size=1,
        search_caption_filter_gap_seconds=0,
        gemini_model="test",
    )

    with (
        patch("app.config.get_settings", return_value=settings),
        patch(
            "app.gemini.caption_filter._filter_batch_with_split",
            new=AsyncMock(return_value=[first]),
        ),
    ):
        results = await filter_images_by_caption_llm(
            "gradutes",
            [first, second],
            preserve_rejected=True,
        )

    assert results == [first, second]
