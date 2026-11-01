"""Resolve startup names/aliases in search queries."""

from __future__ import annotations

import re
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Startup, StartupFaceCluster, StartupFounder
from app.search.local import normalize_person_key


@dataclass(frozen=True)
class ResolvedStartup:
    id: int
    slug: str
    name: str
    aliases: list[str]
    matched_term: str


def _alias_list(startup: Startup) -> list[str]:
    raw = startup.aliases
    aliases: list[str] = []
    if isinstance(raw, list):
        aliases = [str(a).strip() for a in raw if str(a).strip()]
    elif isinstance(raw, dict):
        aliases = [str(a).strip() for a in raw.values() if str(a).strip()]
    aliases.append(startup.name)
    # Dedup case-insensitively while keeping first spelling.
    out: list[str] = []
    seen: set[str] = set()
    for a in aliases:
        key = normalize_person_key(a)
        if key in seen:
            continue
        seen.add(key)
        out.append(a)
    return out


def _term_regex(term: str) -> str:
    parts = [p for p in re.split(r"\s+", term.strip()) if p]
    if not parts:
        return rf"\b{re.escape(term)}\b"
    return r"\b" + r"\s+".join(re.escape(p) for p in parts) + r"\b"


def find_startup_matches_in_query(
    query: str,
    startups: list[Startup],
) -> list[ResolvedStartup]:
    """Longest-alias-first match, similar to find_person_names_in_query."""
    candidates: list[tuple[str, Startup]] = []
    for startup in startups:
        for alias in _alias_list(startup):
            candidates.append((alias, startup))
    candidates.sort(key=lambda pair: len(pair[0]), reverse=True)

    matched_starts: set[int] = set()
    matched_ids: set[int] = set()
    out: list[ResolvedStartup] = []
    for alias, startup in candidates:
        if startup.id in matched_ids:
            continue
        pattern = _term_regex(alias)
        for hit in re.finditer(pattern, query, flags=re.IGNORECASE):
            if hit.start() in matched_starts:
                continue
            # Avoid overlapping starts already claimed by a longer alias.
            if any(hit.start() <= s < hit.end() for s in matched_starts):
                continue
            matched_starts.add(hit.start())
            matched_ids.add(startup.id)
            out.append(
                ResolvedStartup(
                    id=startup.id,
                    slug=startup.slug,
                    name=startup.name,
                    aliases=_alias_list(startup),
                    matched_term=alias,
                )
            )
            break
    return out


def strip_startup_terms(query: str, startups: list[ResolvedStartup]) -> str:
    result = query
    for startup in startups:
        for term in sorted(
            {startup.matched_term, startup.name, *startup.aliases},
            key=len,
            reverse=True,
        ):
            result = re.sub(_term_regex(term), "", result, flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", result).strip(" ,")


async def load_startups(session: AsyncSession) -> list[Startup]:
    return list((await session.execute(select(Startup))).scalars().all())


async def resolve_startups_in_query(
    session: AsyncSession,
    query: str,
    *,
    person_names: list[str] | None = None,
) -> list[ResolvedStartup]:
    """Match startups in the query. Person names keep priority on shared tokens."""
    startups = await load_startups(session)
    if not startups:
        return []
    matches = find_startup_matches_in_query(query, startups)
    if not matches or not person_names:
        return matches

    person_keys = {normalize_person_key(n) for n in person_names}
    filtered: list[ResolvedStartup] = []
    for match in matches:
        # Drop startup match when the matched term is exactly a known person name
        # (e.g. rare collisions). Keep when person and startup are distinct tokens.
        if normalize_person_key(match.matched_term) in person_keys:
            continue
        if normalize_person_key(match.name) in person_keys:
            continue
        filtered.append(match)
    return filtered


async def startups_for_person_names(
    session: AsyncSession,
    person_names: list[str],
) -> list[ResolvedStartup]:
    """Reverse link: named founders → their startups."""
    if not person_names:
        return []
    rows = (
        await session.execute(
            select(Startup, StartupFounder)
            .join(StartupFounder, StartupFounder.startup_id == Startup.id)
            .where(StartupFounder.person_id.is_not(None))
        )
    ).all()
    # Also match by founder_name text against person_names (before person_id link).
    text_rows = (
        await session.execute(
            select(Startup, StartupFounder).join(
                StartupFounder, StartupFounder.startup_id == Startup.id
            )
        )
    ).all()

    person_keys = {normalize_person_key(n) for n in person_names}
    out: dict[int, ResolvedStartup] = {}
    for startup, founder in [*rows, *text_rows]:
        if normalize_person_key(founder.founder_name) not in person_keys:
            # person_id path: need person name resolution — skip unless text matches
            continue
        if startup.id in out:
            continue
        out[startup.id] = ResolvedStartup(
            id=startup.id,
            slug=startup.slug,
            name=startup.name,
            aliases=_alias_list(startup),
            matched_term=founder.founder_name,
        )
    return list(out.values())


async def linked_cluster_ids(
    session: AsyncSession,
    startup_ids: list[int],
) -> list[int]:
    if not startup_ids:
        return []
    rows = (
        await session.execute(
            select(StartupFaceCluster.cluster_id).where(
                StartupFaceCluster.startup_id.in_(startup_ids)
            )
        )
    ).scalars().all()
    return [int(x) for x in rows]
