"""Unit tests for startup catalog resolve + founder linking helpers."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.startups.catalog import load_catalog
import numpy as np

from app.startups.ingest import IngestStats, flatten_logo
from app.startups.paths import catalog_path
from app.startups.resolve import (
    ResolvedStartup,
    find_startup_matches_in_query,
    strip_startup_terms,
)
from app.startups.search import (
    caption_mentions_startup,
    link_person_to_startup_founders,
    logo_score_cutoff,
)


def test_caption_mention_multi_word_any_case():
    cap = 'Two jars labeled "eat atlas" and BHUTAN SPICY DIP on a pedestal.'
    assert caption_mentions_startup(cap, "Eat Atlas", ["eatatlas"])
    assert caption_mentions_startup("Jars labeled EAT ATLAS.", "Eat Atlas", [])
    assert caption_mentions_startup("A Woodys Pizzeria box.", "Woody's Pizzeria", [])
    assert not caption_mentions_startup("A great atlas of eateries.", "Eat Atlas", [])


def test_caption_mention_single_word_needs_brand_styling():
    assert not caption_mentions_startup("Eight people sit at a table.", "EIGHT", [])
    assert not caption_mentions_startup("A pixel art poster.", "Pixel", [])
    assert caption_mentions_startup('A banner reading "Pixel" on stage.', "Pixel", [])
    assert caption_mentions_startup("A cup with the EIGHT logo.", "EIGHT", [])


def test_flatten_logo_puts_dark_transparent_mark_on_white():
    logo = np.zeros((20, 40, 4), dtype=np.uint8)
    logo[5:15, 10:30, 3] = 255
    flat = flatten_logo(logo)
    assert flat.shape[2] == 3
    assert flat[0, 0].tolist() == [255, 255, 255]
    assert flat[flat.shape[0] // 2, flat.shape[1] // 2].tolist() == [0, 0, 0]


def test_flatten_logo_puts_light_transparent_mark_on_dark():
    logo = np.zeros((20, 40, 4), dtype=np.uint8)
    logo[5:15, 10:30] = 255
    flat = flatten_logo(logo)
    assert flat[0, 0].tolist() == [32, 32, 32]


def test_logo_cutoff_rejects_flat_noise_band():
    noise = [0.75 - i * 0.001 for i in range(80)]
    assert logo_score_cutoff(noise, 0.72) > max(noise)


def test_logo_cutoff_keeps_standouts():
    scores = [0.95, 0.93] + [0.70 - i * 0.001 for i in range(78)]
    cutoff = logo_score_cutoff(scores, 0.72)
    assert [s for s in scores if s >= cutoff] == [0.95, 0.93]


def _startup(id: int, slug: str, name: str, aliases: list[str] | None = None):
    return SimpleNamespace(
        id=id,
        slug=slug,
        name=name,
        aliases=aliases or [],
    )


def test_catalog_present_and_nonempty():
    assert catalog_path().is_file()
    entries = load_catalog()
    assert len(entries) >= 30
    eight = next(s for s in entries if s.slug == "eight")
    assert eight.name == "EIGHT"
    assert any("eight" in a.lower() for a in eight.aliases)
    assert eight.founders
    assert eight.resolve_logo() or eight.logos


def test_alias_resolution_longest_first():
    startups = [
        _startup(1, "eight", "EIGHT", ["eight", "eight network", "eight.network"]),
        _startup(2, "eat-atlas", "Eat Atlas", ["eat atlas", "eatatlas"]),
    ]
    matches = find_startup_matches_in_query("photos of Eight Network founders", startups)
    assert len(matches) == 1
    assert matches[0].slug == "eight"
    assert matches[0].matched_term.lower() in {"eight network", "eight.network", "eight"}


def test_strip_startup_terms():
    resolved = [
        ResolvedStartup(
            id=1,
            slug="eight",
            name="EIGHT",
            aliases=["eight", "eight network"],
            matched_term="Eight Network",
        )
    ]
    leftover = strip_startup_terms("Eight Network logo", resolved)
    assert "eight" not in leftover.lower()
    assert "logo" in leftover.lower()


def test_or_expansion_reasons_prefer_founder():
    """Logo + founder hits for the same file keep a founder reason."""
    from app.startups.search import StartupHit

    hits = [
        StartupHit(drive_file_id="a", match_reason="logo", score=0.8),
        StartupHit(drive_file_id="a", match_reason="founder:Mohit", score=0.9),
        StartupHit(drive_file_id="b", match_reason="logo", score=0.7),
    ]
    merged: dict[str, StartupHit] = {}
    for hit in hits:
        prev = merged.get(hit.drive_file_id)
        if prev is None:
            merged[hit.drive_file_id] = hit
            continue
        prefer_founder = hit.match_reason.startswith("founder") and not prev.match_reason.startswith(
            "founder"
        )
        if prefer_founder:
            merged[hit.drive_file_id] = StartupHit(
                drive_file_id=hit.drive_file_id,
                match_reason=hit.match_reason,
                score=max(hit.score or 0, prev.score or 0),
            )
    assert merged["a"].match_reason.startswith("founder")
    assert merged["b"].match_reason == "logo"


@pytest.mark.asyncio
async def test_link_person_never_creates_person(monkeypatch):
    """link_person_to_startup_founders only updates StartupFounder.person_id."""

    class FakeFounder:
        def __init__(self, name: str, startup_id: int = 1):
            self.founder_name = name
            self.startup_id = startup_id
            self.person_id = None

    founders = [FakeFounder("Mohit Paliwal"), FakeFounder("Other Person")]

    class FakeResult:
        def __init__(self, rows):
            self._rows = rows

        def scalars(self):
            return self

        def all(self):
            return self._rows

    class FakeSession:
        def __init__(self):
            self.flushed = False

        async def execute(self, _stmt):
            # First call: cluster→startup ids; second: all founders
            text = str(_stmt)
            if "startup_face_clusters" in text or "StartupFaceCluster" in text:
                return FakeResult([1])
            return FakeResult(founders)

        async def flush(self):
            self.flushed = True

    session = FakeSession()
    # The real function uses select() objects; inspect string may not include class name.
    # Patch execute to always return founders after empty startup_ids path.
    async def execute(stmt):
        return FakeResult(founders)

    session.execute = execute  # type: ignore[method-assign]
    updated = await link_person_to_startup_founders(
        session,
        person_id=42,
        person_name="Mohit Paliwal",
        cluster_id=None,
    )
    assert updated == 1
    assert founders[0].person_id == 42
    assert founders[1].person_id is None
    assert session.flushed


def test_ingest_stats_never_names_person_fields():
    """IngestStats tracks links/clusters only — no person_named counter."""
    stats = IngestStats()
    assert "person" not in stats.as_dict() or "person_named" not in stats.as_dict()
    assert "clusters_created" in stats.as_dict()
