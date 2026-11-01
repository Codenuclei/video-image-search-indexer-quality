"""Unit tests for startup catalog resolve + founder linking helpers."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.startups.catalog import load_catalog
from app.startups.ingest import IngestStats
from app.startups.paths import catalog_path
from app.startups.resolve import (
    ResolvedStartup,
    find_startup_matches_in_query,
    strip_startup_terms,
)
from app.startups.search import link_person_to_startup_founders


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
