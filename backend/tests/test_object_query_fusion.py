from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.schemas import ObjectEvidence
from app.search.images import search_image_files


def _evidence(label: str) -> ObjectEvidence:
    return ObjectEvidence(
        label=label,
        category="apparel",
        confidence=0.95,
        source="caption",
        evidence_text=label,
    )


def _settings() -> SimpleNamespace:
    return SimpleNamespace(
        gemini_api_key="test",
        search_query_expansion=False,
        image_caption_enabled=True,
        gemini_image_result_limit=30,
        gemini_image_min_score=0.0,
        image_visual_weight=0.4,
        image_caption_weight=0.6,
        search_variant_max_parallel=1,
        cpu_thread_pool_size=1,
    )


def _session(*ids: str) -> AsyncMock:
    files = [
        SimpleNamespace(
            id=fid,
            name=f"{fid}.jpg",
            index_name=None,
            path=f"/Images/{fid}.jpg",
            mime_type="image/jpeg",
        )
        for fid in ids
    ]
    session = AsyncMock()
    session.execute.return_value = SimpleNamespace(
        scalars=lambda: SimpleNamespace(all=lambda: files)
    )
    return session


@pytest.mark.asyncio
async def test_branded_tshirt_gate_keeps_only_fully_supported_object_candidate():
    session = _session("branded", "plain", "banner")
    visual_hits = [
        {"drive_file_id": fid, "score": 0.7}
        for fid in ("branded", "plain", "banner")
    ]
    caption_hits = [
        {
            "drive_file_id": "banner",
            "score": 0.75,
            "caption": "A Masters' Union banner hangs on the wall.",
        }
    ]
    object_matches = {
        "branded": [_evidence("t-shirt")],
        "plain": [_evidence("t-shirt")],
    }

    with (
        patch("app.search.images.get_settings", return_value=_settings()),
        patch(
            "app.search.images.get_runtime_settings",
            return_value=SimpleNamespace(search_semantic_min_score=0.32),
        ),
        patch("app.search.images.embed_text_sync", return_value=[0.1, 0.2]),
        patch("app.search.images.search_images_sync", return_value=visual_hits),
        patch("app.search.images.search_captions_sync", return_value=caption_hits),
        patch("app.search.images.search_caption_keywords_sync", return_value=[]),
        patch(
            "app.objects.search.object_matches_for_query",
            new=AsyncMock(return_value=object_matches),
        ),
        patch(
            "app.qdrant.image_captions.get_captions_by_ids_sync",
            return_value={
                "branded": "A person wearing a Masters' Union t-shirt.",
                "plain": "A person wearing a plain black t-shirt.",
            },
        ) as captions_by_id,
        patch(
            "app.search.images.person_names_for_drive_files",
            new=AsyncMock(return_value={}),
        ),
    ):
        results = await search_image_files(
            session,
            "t-shirt mastersunion",
            use_captions=True,
        )

    assert [item.drive_file_id for item in results] == ["branded"]
    assert captions_by_id.call_count == 2
    assert set(captions_by_id.call_args_list[0].args[0]) == {
        "branded",
        "plain",
        "banner",
    }


@pytest.mark.asyncio
async def test_single_object_search_keeps_existing_exact_object_bypass():
    session = _session("plain")
    with (
        patch("app.search.images.get_settings", return_value=_settings()),
        patch(
            "app.search.images.get_runtime_settings",
            return_value=SimpleNamespace(search_semantic_min_score=0.32),
        ),
        patch("app.search.images.embed_text_sync", return_value=[0.1, 0.2]),
        patch(
            "app.search.images.search_images_sync",
            return_value=[{"drive_file_id": "plain", "score": 0.1}],
        ),
        patch("app.search.images.search_captions_sync", return_value=[]),
        patch("app.search.images.search_caption_keywords_sync", return_value=[]),
        patch(
            "app.objects.search.object_matches_for_query",
            new=AsyncMock(return_value={"plain": [_evidence("t-shirt")]}),
        ),
        patch(
            "app.qdrant.image_captions.get_captions_by_ids_sync",
            return_value={},
        ) as captions_by_id,
        patch(
            "app.search.images.person_names_for_drive_files",
            new=AsyncMock(return_value={}),
        ),
    ):
        results = await search_image_files(session, "t-shirt", use_captions=True)

    assert [item.drive_file_id for item in results] == ["plain"]
    assert results[0].score > 0.9
    captions_by_id.assert_called_once()


@pytest.mark.asyncio
async def test_specialized_pool_can_disable_conjunctive_object_gate():
    session = _session("plain")
    with (
        patch("app.search.images.get_settings", return_value=_settings()),
        patch(
            "app.search.images.get_runtime_settings",
            return_value=SimpleNamespace(search_semantic_min_score=0.32),
        ),
        patch("app.search.images.embed_text_sync", return_value=[0.1, 0.2]),
        patch(
            "app.search.images.search_images_sync",
            return_value=[{"drive_file_id": "plain", "score": 0.1}],
        ),
        patch("app.search.images.search_captions_sync", return_value=[]),
        patch("app.search.images.search_caption_keywords_sync", return_value=[]),
        patch(
            "app.objects.search.object_matches_for_query",
            new=AsyncMock(return_value={"plain": [_evidence("t-shirt")]}),
        ),
        patch(
            "app.qdrant.image_captions.get_captions_by_ids_sync",
            return_value={},
        ) as captions_by_id,
        patch(
            "app.search.images.person_names_for_drive_files",
            new=AsyncMock(return_value={}),
        ),
    ):
        results = await search_image_files(
            session,
            "t-shirt mastersunion",
            use_captions=True,
            enable_conjunctive_object_gate=False,
        )

    assert [item.drive_file_id for item in results] == ["plain"]
    captions_by_id.assert_called_once()
