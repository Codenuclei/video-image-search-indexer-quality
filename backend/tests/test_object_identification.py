from __future__ import annotations

from contextlib import nullcontext
from types import SimpleNamespace

import pytest

from app.objects.search import fuse_object_score
from app.objects.query_concepts import (
    all_query_concepts_supported,
    parse_query_concepts,
    text_supports_concept,
)
from app.objects.taxonomy import (
    OBJECT_MODEL_VERSION,
    TAXONOMY_VERSION,
    canonicalize_object,
    classify_text,
    object_query_labels,
)
from app.workers.object_queue import _merge_labels
from tests.conftest import requires_postgres


def test_apparel_aliases_are_canonical_and_versioned() -> None:
    assert canonicalize_object("tshirt") == "t-shirt"
    assert canonicalize_object("tee") == "t-shirt"
    assert canonicalize_object("sports shirt") == "jersey"
    assert canonicalize_object("kit") == "jersey"
    assert "jersey" in object_query_labels("students wearing team kits")
    assert TAXONOMY_VERSION in OBJECT_MODEL_VERSION


def test_query_concepts_use_longest_non_overlapping_alias() -> None:
    concepts = parse_query_concepts("t-shirt mastersunion")
    assert concepts.taxonomy_labels == ("t-shirt",)
    assert concepts.residual_terms == ("mastersunion",)
    assert object_query_labels("t-shirt mastersunion") == ("t-shirt",)


def test_search_scaffolding_does_not_make_single_object_query_conjunctive() -> None:
    concepts = parse_query_concepts("show photos of people wearing t-shirts")
    assert concepts.taxonomy_labels == ("t-shirt",)
    assert concepts.residual_terms == ()
    assert concepts.is_conjunctive_object_query is False


def test_query_concept_matching_normalizes_apostrophes_spaces_and_compact_forms() -> None:
    assert text_supports_concept("A Masters' Union logo", "mastersunion")
    assert text_supports_concept("A MastersUnion logo", "masters union")
    assert text_supports_concept("person wearing a t-shirt", "tshirt")


def test_conjunctive_support_requires_brand_and_every_taxonomy_label() -> None:
    branded_shirt = parse_query_concepts("mastersunion t-shirt")
    assert all_query_concepts_supported(
        branded_shirt,
        structured_labels=("t-shirt",),
        caption="A person wears a Masters' Union tee.",
    )
    assert not all_query_concepts_supported(
        branded_shirt,
        structured_labels=("t-shirt",),
        caption="A person wears a plain tee.",
    )
    assert not all_query_concepts_supported(
        branded_shirt,
        structured_labels=("sign",),
        caption="A Masters' Union banner hangs on a wall.",
    )

    two_objects = parse_query_concepts("t-shirt with phone")
    assert all_query_concepts_supported(
        two_objects,
        structured_labels=("t-shirt", "phone"),
    )
    assert not all_query_concepts_supported(
        two_objects,
        structured_labels=("t-shirt",),
    )


def test_caption_classifier_uses_phrase_boundaries() -> None:
    labels = {item["canonical_label"] for item in classify_text(
        "A person in a red tee holds a phone beside a sports shirt."
    )}
    assert {"red", "t-shirt", "phone", "jersey"}.issubset(labels)
    assert classify_text("A credit report and blueprint") == []


def test_video_caption_hits_aggregate_with_best_timestamp() -> None:
    labels = _merge_labels(
        (
            ("A red jersey and football", 4.0),
            ("The same jersey is visible", 9.0),
        ),
        [],
        max_labels=10,
        confidence_floor=0.7,
    )
    jersey = next(item for item in labels if item["canonical_label"] == "jersey")
    assert jersey["best_timestamp"] == 4.0
    assert jersey["hit_count"] == 2


def test_exact_object_boost_is_deterministic_and_bounded() -> None:
    assert fuse_object_score(0.4, 1) == pytest.approx(0.98)
    assert fuse_object_score(0.4, 1, 0.5) == pytest.approx(0.89)
    assert fuse_object_score(0.95, 3) == 0.99
    assert fuse_object_score(0.4, 0) == 0.4


def test_embedding_threshold_and_video_timestamp(monkeypatch) -> None:
    import numpy as np
    from app.objects import vectors

    monkeypatch.setattr(
        vectors,
        "_taxonomy_cache",
        ([("phone", "electronics"), ("chair", "furniture")], np.eye(2, dtype=np.float32)),
    )
    labels = vectors.classify_vectors(
        [(3.0, [1.0, 0.0]), (8.0, [0.8, 0.2])],
        confidence_floor=0.9,
    )
    phone = next(item for item in labels if item["canonical_label"] == "phone")
    assert phone["best_timestamp"] == 3.0
    assert phone["hit_count"] == 2
    assert not any(item["canonical_label"] == "chair" for item in labels)


def test_taxonomy_text_embeddings_send_one_content_per_label(monkeypatch) -> None:
    from app.gemini import video_embeddings

    captured: dict[str, object] = {}

    class Models:
        def embed_content(self, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(
                embeddings=[
                    SimpleNamespace(values=[1.0, 0.0]),
                    SimpleNamespace(values=[0.0, 1.0]),
                ]
            )

    monkeypatch.setattr(
        video_embeddings,
        "_get_client",
        lambda: SimpleNamespace(models=Models()),
    )
    monkeypatch.setattr(video_embeddings, "gemini_embed_slot", nullcontext)

    vectors = video_embeddings.embed_texts_batch_sync(["a jersey", "a t-shirt"])

    assert vectors == [[1.0, 0.0], [0.0, 1.0]]
    contents = captured["contents"]
    assert len(contents) == 2
    assert contents[0].parts[0].text == "a jersey"
    assert contents[1].parts[0].text == "a t-shirt"


def test_object_runtime_defaults_are_canary_safe() -> None:
    from app.runtime_settings import _env_defaults

    runtime = _env_defaults()
    assert runtime.object_lane_enabled is False
    assert runtime.object_backfill_enabled is False
    assert runtime.object_batch_size <= 8
    assert runtime.object_face_priority_ratio >= 1


@requires_postgres
@pytest.mark.asyncio
async def test_object_enqueue_is_idempotent_and_version_aware(db_session) -> None:
    from app.db.models import DriveFile, DriveFileStatus, Media, MediaType, ObjectJob
    from app.workers.object_queue import enqueue_object_job
    from sqlalchemy import func, select

    db_session.add(
        DriveFile(
            id="object-test",
            name="object.jpg",
            path="/object.jpg",
            mime_type="image/jpeg",
            status=DriveFileStatus.PROCESSED,
        )
    )
    await db_session.flush()
    db_session.add(Media(drive_file_id="object-test", type=MediaType.IMAGE))
    await db_session.flush()

    first = await enqueue_object_job(db_session, "object-test")
    second = await enqueue_object_job(db_session, "object-test")
    assert first is second
    assert await db_session.scalar(select(func.count(ObjectJob.id))) == 1

    await enqueue_object_job(db_session, "object-test", model_version="objects-v2")
    assert await db_session.scalar(select(func.count(ObjectJob.id))) == 2
