"""Load the committed startups catalog."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any

from app.startups.paths import asset_path, catalog_path


@dataclass(frozen=True)
class CatalogFounder:
    name: str


@dataclass(frozen=True)
class CatalogStartup:
    slug: str
    name: str
    aliases: list[str] = field(default_factory=list)
    cohort: str | None = None
    sector: str | None = None
    one_liner: str | None = None
    founders: list[CatalogFounder] = field(default_factory=list)
    candidate_founders: list[str] = field(default_factory=list)
    pages: list[int] = field(default_factory=list)
    logo: str | None = None
    logos: list[str] = field(default_factory=list)
    founder_photos: list[str] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)

    def resolve_logo(self) -> str | None:
        for rel in [self.logo, *self.logos]:
            if not rel:
                continue
            path = asset_path(rel)
            if path.is_file():
                return rel
        return None

    def resolve_founder_photos(self) -> list[str]:
        out: list[str] = []
        for rel in self.founder_photos:
            if rel and asset_path(rel).is_file():
                out.append(rel)
        return out


def _parse_startup(raw: dict[str, Any]) -> CatalogStartup:
    assets = raw.get("assets") if isinstance(raw.get("assets"), dict) else {}
    founders_raw = raw.get("founders") or []
    founders: list[CatalogFounder] = []
    for item in founders_raw:
        if isinstance(item, str) and item.strip():
            founders.append(CatalogFounder(name=item.strip()))
        elif isinstance(item, dict) and str(item.get("name") or "").strip():
            founders.append(CatalogFounder(name=str(item["name"]).strip()))
    candidates = [
        str(x).strip()
        for x in (raw.get("candidate_founders") or [])
        if str(x).strip()
    ]
    return CatalogStartup(
        slug=str(raw.get("slug") or "").strip(),
        name=str(raw.get("name") or "").strip(),
        aliases=[str(a).strip() for a in (raw.get("aliases") or []) if str(a).strip()],
        cohort=(str(raw["cohort"]).strip() if raw.get("cohort") else None),
        sector=(str(raw["sector"]).strip() if raw.get("sector") else None),
        one_liner=(str(raw["one_liner"]).strip() if raw.get("one_liner") else None),
        founders=founders,
        candidate_founders=candidates,
        pages=[int(p) for p in (raw.get("pages") or []) if str(p).isdigit() or isinstance(p, int)],
        logo=(str(assets["logo"]).strip() if assets.get("logo") else None),
        logos=[str(x).strip() for x in (assets.get("logos") or []) if str(x).strip()],
        founder_photos=[
            str(x).strip() for x in (assets.get("founder_photos") or []) if str(x).strip()
        ],
        sources=[str(x).strip() for x in (raw.get("sources") or []) if str(x).strip()],
    )


@lru_cache(maxsize=1)
def load_catalog() -> tuple[CatalogStartup, ...]:
    path = catalog_path()
    if not path.is_file():
        return tuple()
    payload = json.loads(path.read_text(encoding="utf-8"))
    rows = payload.get("startups") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        return tuple()
    out: list[CatalogStartup] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        startup = _parse_startup(row)
        if startup.slug and startup.name:
            out.append(startup)
    return tuple(out)


def reload_catalog() -> tuple[CatalogStartup, ...]:
    load_catalog.cache_clear()
    return load_catalog()
