"""Guards so identify/caption experiments cannot break GET /search."""

from __future__ import annotations

from inspect import signature
from pathlib import Path

from app.gemini import captions as caption_mod
from app.objects.identify_tags import IdentifyLabel, IdentifyResult, parse_identify_output, persist_rows
from app.qwen import vlm as vlm_mod
from app.runtime_settings import _env_defaults
from app.routers import search as search_mod
from app.search.images import search_image_files


def test_production_gemini_captions_do_not_use_identify_prompt() -> None:
    src = Path(caption_mod.__file__).read_text()
    assert "CAPTION_PROMPT" not in src
    assert "IDENTIFY_AND_CAPTION_PROMPT" not in src
    assert "80-140 words" not in caption_mod._DESCRIBE_INSTRUCTION
    assert "JSON array of strings" in caption_mod._DESCRIBE_INSTRUCTION


def test_production_qwen_frame_captions_stay_one_sentence() -> None:
    src = Path(vlm_mod.__file__).read_text()
    assert "from app.objects.identify_tags import" not in src
    assert "CAPTION_PROMPT" not in src
    assert "one concise sentence" in vlm_mod._DESCRIBE_PROMPT


def test_get_search_defaults_to_production_policy() -> None:
    assert search_mod.search.__name__ == "search"
    params = signature(search_mod._run_search).parameters
    assert params["retrieval_policy"].default == "default"


def test_image_search_identify_flag_defaults_off() -> None:
    params = signature(search_image_files).parameters
    assert params["retrieval_policy"].default == "default"


def test_identify_lane_stays_off_by_default() -> None:
    assert _env_defaults().identify_lane_enabled is False
    assert _env_defaults().ocr_lane_enabled is False


def test_mixed_caption_never_persists_to_identify_labels() -> None:
    parsed = parse_identify_output(
        "OBJECTS\ngraduation cap | mortarboard\n"
        "ACTIONS\nposing with graduation cap\n"
        "CAPTION\nPeople are dining at a modern restaurant buffet, seated around a "
        "speckled table filled with various dishes and a hanging graduation cap.\n"
    )
    assert "restaurant buffet" in parsed.caption
    rows = persist_rows(parsed)
    blob = " ".join(str(row["evidence_text"]) for row in rows)
    assert "speckled table" not in blob
    assert all(len(str(row["canonical_label"])) <= 96 for row in rows)
    assert all(len(str(row["evidence_text"])) <= 240 for row in rows)
    assert "graduation cap" in {row["canonical_label"] for row in rows}


def test_persist_rows_clips_varchar_and_ignores_caption_kind() -> None:
    parsed = IdentifyResult(
        objects=[IdentifyLabel(kind="object", label="x" * 200, synonyms=("y" * 80,))],
        actions=[IdentifyLabel(kind="caption", label="must not persist", synonyms=())],
        caption="People are dining at a restaurant. " * 20,
    )
    rows = persist_rows(parsed)
    assert rows
    assert all(len(str(row["canonical_label"])) <= 96 for row in rows)
    assert all(len(str(row["category"])) <= 48 for row in rows)
    assert all(len(str(row["evidence_source"])) <= 32 for row in rows)
    assert all(len(str(row["evidence_text"] or "")) <= 240 for row in rows)
    assert all(len(str(row["model_version"])) <= 96 for row in rows)
    blob = " ".join(str(row["canonical_label"]) for row in rows)
    assert "must not persist" not in blob
    assert "dining at a restaurant" not in blob
