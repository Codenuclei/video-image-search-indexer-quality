from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.objects.identify_tags import (
    EXPERIMENTAL_OVERLAY_NAME,
    QWEN_IDENTIFY_MODEL_VERSION,
    experimental_caption_match,
    file_covers_identify_query,
    filter_tags,
    labels_from_legacy_tags,
    load_local_identify_overlay,
    merge_identify_lanes,
    parse_identify_output,
    parse_identify_query,
    persist_rows,
    rerank_shortlist_with_identify,
)
from app.schemas import ObjectEvidence
from app.search.images import search_image_files


def test_filter_tags_still_drops_generic_body_parts() -> None:
    tags = filter_tags("eye\nmouth\nblack shirt\nNike")
    assert tags == ["black shirt", "nike"]


def test_parse_keeps_objects_and_actions_separate_with_synonyms() -> None:
    text = """
    OBJECTS
    ceremonial cheque | check, oversized check, award cheque
    navy blazer | blue blazer, navy jacket

    ACTIONS
    giving ceremonial cheque | handing cheque, presenting cheque
    wearing navy blazer | dressed in navy blazer
    """
    parsed = parse_identify_output(text)
    assert [item.label for item in parsed.objects] == ["ceremonial cheque", "navy blazer"]
    assert "check" in parsed.objects[0].synonyms
    assert [item.label for item in parsed.actions] == [
        "giving ceremonial cheque",
        "wearing navy blazer",
    ]
    assert "handing cheque" in parsed.actions[0].synonyms
    merged = merge_identify_lanes(parsed.objects, parsed.actions).merged
    assert [item.kind for item in merged] == ["object", "object", "action", "action"]
    rows = persist_rows(parsed)
    labels = {row["canonical_label"] for row in rows}
    assert "ceremonial cheque" in labels
    assert "check" in labels
    assert "giving" in labels
    assert "handing" in labels
    assert {row["model_version"] for row in rows} == {QWEN_IDENTIFY_MODEL_VERSION}
    sources = {row["evidence_source"] for row in rows}
    assert "qwen_identify" in sources
    assert "qwen_action" in sources
    assert "qwen_synonym" in sources


def test_bare_gerunds_are_dropped_but_verb_object_actions_kept() -> None:
    parsed = parse_identify_output("ACTIONS\nholding\nholding trophy | carrying trophy")
    assert [item.label for item in parsed.actions] == ["holding trophy"]
    assert "carrying trophy" in parsed.actions[0].synonyms


def test_giving_cheque_query_matches_handing_check_synonyms() -> None:
    query = parse_identify_query("giving cheque")
    assert "giving" in query.action_terms
    assert "handing" in query.action_terms
    assert "cheque" in query.object_terms
    assert "check" in query.object_terms
    assert query.requires_both is True
    assert file_covers_identify_query({"handing", "check"}, query)
    assert not file_covers_identify_query({"check"}, query)
    assert not file_covers_identify_query({"giving"}, query)


def test_cooking_query_is_action_only() -> None:
    query = parse_identify_query("students cooking")
    assert "cooking" in query.action_terms
    assert query.requires_both is False
    assert file_covers_identify_query({"cooking", "chopping"}, query)


def test_legacy_identify_json_splits_wearing_into_action_lane() -> None:
    parsed = labels_from_legacy_tags(
        ["wearing navy blazer", "hyrox delhi", "black shirt"],
        raw_text="",
    )
    assert [item.label for item in parsed.actions] == ["wearing navy blazer"]
    assert "hyrox delhi" in [item.label for item in parsed.objects]
    query = parse_identify_query("wearing navy blazer")
    phrases = []
    for item in persist_rows(parsed):
        phrases.append(str(item["canonical_label"]))
    assert file_covers_identify_query(phrases, query)
    query = parse_identify_query("students cooking")
    assert "cooking" in query.action_terms
    assert query.requires_both is False
    assert file_covers_identify_query({"cooking", "chopping"}, query)


def _evidence(label: str, category: str = "object") -> ObjectEvidence:
    return ObjectEvidence(
        label=label,
        category=category,
        confidence=0.92,
        source="qwen_identify" if category == "object" else "qwen_action",
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
        image_visual_strong_score=0.5,
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
async def test_production_action_search_still_drops_uncaptioned_object_only_hit():
    session = _session("cheque1")
    with (
        patch("app.search.images.get_settings", return_value=_settings()),
        patch(
            "app.search.images.get_runtime_settings",
            return_value=SimpleNamespace(search_semantic_min_score=0.32),
        ),
        patch("app.search.images.embed_text_sync", return_value=[0.1, 0.2]),
        patch("app.search.images.search_images_sync", return_value=[]),
        patch("app.search.images.search_captions_sync", return_value=[]),
        patch("app.search.images.search_caption_keywords_sync", return_value=[]),
        patch(
            "app.objects.search.object_matches_for_query",
            new=AsyncMock(
                return_value={
                    "cheque1": [
                        _evidence("giving", "action"),
                        _evidence("cheque", "object"),
                    ]
                }
            ),
        ),
        patch("app.qdrant.image_captions.get_captions_by_ids_sync", return_value={}),
        patch(
            "app.search.images.person_names_for_drive_files",
            new=AsyncMock(return_value={}),
        ),
    ):
        results = await search_image_files(
            session,
            "giving cheque",
            use_captions=True,
            action_query=True,
        )
    assert results == []


@pytest.mark.asyncio
async def test_testv2_keeps_action_object_hit_without_caption():
    session = _session("cheque1")
    with (
        patch("app.search.images.get_settings", return_value=_settings()),
        patch(
            "app.search.images.get_runtime_settings",
            return_value=SimpleNamespace(search_semantic_min_score=0.32),
        ),
        patch("app.search.images.embed_text_sync", return_value=[0.1, 0.2]),
        patch("app.search.images.search_images_sync", return_value=[]),
        patch("app.search.images.search_captions_sync", return_value=[]),
        patch("app.search.images.search_caption_keywords_sync", return_value=[]),
        patch(
            "app.objects.search.object_matches_for_query",
            new=AsyncMock(
                return_value={
                    "cheque1": [
                        _evidence("giving", "action"),
                        _evidence("cheque", "object"),
                    ]
                }
            ),
        ),
        patch("app.qdrant.image_captions.get_captions_by_ids_sync", return_value={}),
        patch(
            "app.search.images.person_names_for_drive_files",
            new=AsyncMock(return_value={}),
        ),
    ):
        results = await search_image_files(
            session,
            "giving cheque",
            use_captions=True,
            action_query=True,
            retrieval_policy="testv2",
        )
    assert [item.drive_file_id for item in results] == ["cheque1"]
    assert results[0].matched_objects
    kinds = {item.category for item in results[0].matched_objects}
    assert "action" in kinds
    assert "object" in kinds


def test_experimental_overlay_preferred_over_legacy_identify_json(tmp_path) -> None:
    (tmp_path / "qwen_sglang_identify_zzzz.json").write_text(
        json.dumps(
            {
                "results": [
                    {
                        "drive_file_id": "legacy",
                        "tags": ["navy blazer"],
                        "raw_text": "navy blazer",
                    }
                ]
            }
        )
    )
    (tmp_path / EXPERIMENTAL_OVERLAY_NAME).write_text(
        json.dumps(
            {
                "results": [
                    {
                        "drive_file_id": "prod-hit",
                        "objects": [{"label": "ceremonial cheque", "synonyms": ["check"]}],
                        "actions": [{"label": "handing cheque", "synonyms": ["giving cheque"]}],
                    }
                ]
            }
        )
    )
    overlay = load_local_identify_overlay(tmp_path)
    assert "prod-hit" in overlay
    assert "legacy" not in overlay
    assert [item.label for item in overlay["prod-hit"].objects] == ["ceremonial cheque"]
    assert [item.label for item in overlay["prod-hit"].actions] == ["handing cheque"]


def test_experimental_rerank_keeps_production_order_inside_groups() -> None:
    overlay = {
        "later": parse_identify_output(
            "OBJECTS\nceremonial cheque | check\nACTIONS\nhanding cheque | giving cheque"
        )
    }
    files = [
        {"drive_file_id": "first", "name": "unrelated.jpg"},
        {"drive_file_id": "later", "name": "cheque.jpg"},
        {"drive_file_id": "also", "name": "other.jpg"},
    ]
    ranked = rerank_shortlist_with_identify(
        "person handing over a ceremonial cheque",
        files,
        overlay,
    )
    assert [item["drive_file_id"] for item in ranked] == ["later", "first", "also"]
    assert ranked[0]["qwen_match"] is True
    assert ranked[1]["qwen_match"] is False
    assert "handing cheque" in ranked[0]["qwen_actions"]


def test_experimental_caption_match_keeps_distinctive_hits() -> None:
    assert experimental_caption_match(
        "A chef cooks over an open flame in a commercial kitchen.",
        "students cooking food in a campus kitchen",
    )
    assert experimental_caption_match(
        "A group of people pose in a brightly lit industrial kitchen.",
        "students cooking food in a campus kitchen",
    )
    assert experimental_caption_match(
        "A woman jogs under a large masters' union HYROX DELHI sign.",
        "hyrox delhi masters union race backdrop",
    )
    assert experimental_caption_match(
        "A man in a navy blazer stands at a podium on a stage.",
        "people wearing a navy blazer on stage",
    )
    assert experimental_caption_match(
        "Five people hold a large ceremonial check on a stage.",
        "person handing over a ceremonial cheque",
    )
    assert experimental_caption_match(
        "Four people stand on a stage holding a large novelty check.",
        "person handing over a ceremonial cheque",
    )
    assert not experimental_caption_match(
        "A man speaks at a podium in a conference hall.",
        "students cooking food in a campus kitchen",
    )
    from app.objects.identify_tags import experimental_evidence_score

    both = experimental_evidence_score(
        "Five people handing a ceremonial cheque on stage.",
        "person handing over a ceremonial cheque",
    )
    object_only = experimental_evidence_score(
        "A printed check for two lakh rupees sits on a table.",
        "person handing over a ceremonial cheque",
    )
    assert both > object_only > 0


def test_production_lexical_still_requires_every_query_token() -> None:
    from app.qdrant.image_captions import caption_matches_query_text

    assert not caption_matches_query_text(
        "A woman jogs under a large HYROX DELHI sign.",
        "hyrox delhi masters union race backdrop",
    )


@pytest.mark.asyncio
async def test_testv2_keeps_lexical_kitchen_caption() -> None:
    session = _session("kitchen1")
    with (
        patch("app.search.images.get_settings", return_value=_settings()),
        patch(
            "app.search.images.get_runtime_settings",
            return_value=SimpleNamespace(search_semantic_min_score=0.32),
        ),
        patch("app.search.images.embed_text_sync", return_value=[0.1, 0.2]),
        patch("app.search.images.search_images_sync", return_value=[]),
        patch("app.search.images.search_captions_sync", return_value=[]),
        patch(
            "app.search.images.search_caption_keywords_sync",
            return_value=[
                {
                    "drive_file_id": "kitchen1",
                    "caption": "A chef cooks over an open flame in a kitchen.",
                }
            ],
        ),
        patch(
            "app.objects.search.object_matches_for_query",
            new=AsyncMock(return_value={}),
        ),
        patch(
            "app.qdrant.image_captions.get_captions_by_ids_sync",
            return_value={"kitchen1": "A chef cooks over an open flame in a kitchen."},
        ),
        patch(
            "app.search.images.person_names_for_drive_files",
            new=AsyncMock(return_value={}),
        ),
    ):
        results = await search_image_files(
            session,
            "students cooking food in a campus kitchen",
            use_captions=True,
            action_query=True,
            retrieval_policy="testv2",
        )
    assert [item.drive_file_id for item in results] == ["kitchen1"]
    assert results[0].caption
