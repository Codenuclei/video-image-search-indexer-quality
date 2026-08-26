"""Stale carousel locks must not 409 theme generation or block a new claim forever."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.routers import carousel_script


class _Result:
    def __init__(self, rowcount: int):
        self.rowcount = rowcount


class _Session:
    def __init__(self, *, first_claim=0, steal=1, second_claim=1, exists=True):
        self.first_claim = first_claim
        self.steal = steal
        self.second_claim = second_claim
        self.exists = exists
        self.commits = 0
        self.calls = 0

    async def execute(self, _statement):
        self.calls += 1
        # claim, optional steal, optional second claim
        if self.calls == 1:
            return _Result(self.first_claim)
        if self.calls == 2:
            return _Result(self.steal)
        return _Result(self.second_claim)

    async def get(self, _model, _key):
        return SimpleNamespace(id="vid") if self.exists else None

    async def commit(self):
        self.commits += 1


@pytest.mark.asyncio
async def test_claim_steals_stale_lock_then_succeeds() -> None:
    session = _Session(first_claim=0, steal=1, second_claim=1)
    token = await carousel_script._claim_carousel(session, "vid")
    assert token
    assert session.commits >= 1


@pytest.mark.asyncio
async def test_claim_still_409s_when_lock_is_fresh() -> None:
    session = _Session(first_claim=0, steal=0, second_claim=0, exists=True)
    with pytest.raises(HTTPException) as exc:
        await carousel_script._claim_carousel(session, "vid")
    assert exc.value.status_code == 409


@pytest.mark.asyncio
async def test_steal_helper_commits_when_row_updated() -> None:
    session = _Session(first_claim=1)  # execute once → treat as steal rowcount
    session.calls = 0
    stolen = await carousel_script._steal_stale_carousel_lock(session, "vid")
    assert stolen is True
    assert session.commits == 1
