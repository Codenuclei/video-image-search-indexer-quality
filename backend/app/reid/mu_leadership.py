"""Scrape Masters' Union About Us leadership tabs and reverse-search faces."""
from __future__ import annotations

import logging
import re
from typing import Any
from urllib.parse import urljoin

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

MU_ABOUT_URL = "https://mastersunion.org/about-us"

# data-rel / section id on https://mastersunion.org/about-us
LEADERSHIP_TABS = {
    "board": ("master1", "Board of governors"),
    "executive": ("master2", "Executive Leaders"),
    "faculty": ("master3", "Masters in residence"),
}

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (compatible; DriveFaceIndexer/1.0; +https://mastersunion.org)"
    ),
    "Accept": "text/html,application/xhtml+xml",
}


def _extract_section(html: str, section_id: str) -> str:
    """Return HTML for id=\"section_id\" until the next id=\"masterN\" or end of wrapper."""
    pattern = rf'id="{re.escape(section_id)}"([\s\S]*?)(?:id="master\d+"|</div>\s*</div>\s*</div>\s*</div>\s*<div class="content|$)'
    m = re.search(pattern, html, flags=re.IGNORECASE)
    if not m:
        # fallback: until next master id only
        m = re.search(
            rf'id="{re.escape(section_id)}"([\s\S]*?)id="master\d+"',
            html,
            flags=re.IGNORECASE,
        )
    if not m:
        raise ValueError(f"Section {section_id!r} not found on About Us page")
    return m.group(1)


def parse_leadership_cards(section_html: str, base_url: str = MU_ABOUT_URL) -> list[dict[str, str]]:
    """Parse masterCardBoxi cards → name, role, image_url, linkedin_url."""
    people: list[dict[str, str]] = []
    parts = re.split(r'class="masterCardBoxi"', section_html, flags=re.IGNORECASE)
    for part in parts[1:]:
        name_m = re.search(r'class="masterName">([^<]+)', part)
        if not name_m:
            continue
        name = name_m.group(1).strip()
        role_m = re.search(r'class="designationOfMaster">([^<]+)', part)
        role = role_m.group(1).strip() if role_m else ""
        li_m = re.search(
            r'href="(https?://(?:www\.)?linkedin\.com/in/[^"]+)"',
            part,
            flags=re.IGNORECASE,
        )
        linkedin = li_m.group(1).strip() if li_m else ""
        image_url = ""
        for img_m in re.finditer(r'<img[^>]+src="([^"]+)"', part, flags=re.IGNORECASE):
            src = img_m.group(1).strip()
            low = src.lower()
            if "linkedin" in low or low.endswith(".svg") or "svg1.png" in low:
                continue
            if "mastersunion" in low or low.startswith("http") or low.startswith("/"):
                image_url = urljoin(base_url, src)
                break
        people.append(
            {
                "name": name,
                "role": role,
                "image_url": image_url,
                "linkedin_url": linkedin,
            }
        )
    return people


async def fetch_leadership_roster(
    tab: str = "executive",
    *,
    page_url: str = MU_ABOUT_URL,
) -> dict[str, Any]:
    """Live-scrape About Us and return people for the given tab key."""
    if tab not in LEADERSHIP_TABS:
        raise ValueError(f"Unknown tab {tab!r}; choose from {list(LEADERSHIP_TABS)}")
    section_id, label = LEADERSHIP_TABS[tab]
    async with httpx.AsyncClient(timeout=60.0, follow_redirects=True, headers=_HEADERS) as client:
        resp = await client.get(page_url)
        resp.raise_for_status()
        html = resp.text
    section = _extract_section(html, section_id)
    people = parse_leadership_cards(section, base_url=page_url)
    return {
        "source_url": page_url,
        "tab": tab,
        "section_id": section_id,
        "label": label,
        "count": len(people),
        "people": people,
    }


async def scan_leadership_faces(
    session: AsyncSession,
    *,
    tab: str = "executive",
    page_url: str = MU_ABOUT_URL,
    match_limit: int = 8,
    run_web_reverse: bool = False,
) -> dict[str, Any]:
    """
    Scrape leadership photos from About Us, then for each portrait:
    1) match against internal pgvector faces
    2) optionally run Apify/Lens reverse search (slow; off by default)
    """
    from app.reid.face_search import search_faces_by_image_bytes
    from app.reid.reverse_search import reverse_search_face

    roster = await fetch_leadership_roster(tab, page_url=page_url)
    results: list[dict[str, Any]] = []

    async with httpx.AsyncClient(timeout=45.0, follow_redirects=True, headers=_HEADERS) as client:
        for person in roster["people"]:
            item: dict[str, Any] = {
                "name": person["name"],
                "role": person["role"],
                "image_url": person["image_url"],
                "linkedin_url": person.get("linkedin_url") or None,
                "ok": False,
            }
            image_url = person.get("image_url") or ""
            if not image_url:
                item["error"] = "No portrait URL on page"
                results.append(item)
                continue
            try:
                resp = await client.get(image_url)
                resp.raise_for_status()
                search = await search_faces_by_image_bytes(
                    session, resp.content, limit=match_limit
                )
                item["ok"] = True
                item["internal_matches"] = search.get("matches") or []
                item["faces_detected"] = search.get("faces_detected")
                # Best internal person name for comparison with page label
                top = (item["internal_matches"] or [None])[0]
                if top and top.get("person_name"):
                    item["matched_person"] = top.get("person_name")
                    item["match_score"] = top.get("score")
                    item["name_alignment"] = (
                        person["name"].lower().split()[0]
                        in str(top.get("person_name") or "").lower()
                    )
                if run_web_reverse and item["internal_matches"]:
                    # Enrich via reverse-search on the best matched indexed face
                    best_face = item["internal_matches"][0].get("face_id")
                    if best_face:
                        try:
                            item["web_reverse"] = await reverse_search_face(
                                session, int(best_face)
                            )
                        except Exception as exc:  # noqa: BLE001
                            item["web_reverse_error"] = str(exc)[:240]
            except Exception as exc:  # noqa: BLE001
                logger.warning("leadership scan failed for %s: %s", person.get("name"), exc)
                item["error"] = str(exc)[:300]
            results.append(item)

    matched = sum(1 for r in results if r.get("ok") and r.get("internal_matches"))
    return {
        **{k: roster[k] for k in ("source_url", "tab", "section_id", "label", "count")},
        "matched": matched,
        "results": results,
    }


async def name_tag_from_website(
    session: AsyncSession,
    *,
    name: str,
    cluster_ids: list[int] | None = None,
    face_ids: list[int] | None = None,
    role: str | None = None,
) -> dict[str, Any]:
    """
    Create or reuse a Person with the website name, then link matched clusters/faces.

    - Unknown clusters → name_cluster / merge into existing person
    - Individual faces → tag_face_manual (append-only link)
    """
    from sqlalchemy import func, select

    from app.db.models import Face, FaceCluster, Person
    from app.matching.service import merge_cluster_into_person, name_cluster, tag_face_manual

    clean = (name or "").strip()
    if not clean:
        raise ValueError("Name cannot be empty")

    cluster_ids = [int(c) for c in (cluster_ids or []) if c is not None]
    face_ids = [int(f) for f in (face_ids or []) if f is not None]
    if not cluster_ids and not face_ids:
        raise ValueError("Provide at least one cluster_id or face_id to name-tag")

    existing = (
        await session.execute(select(Person).where(func.lower(Person.name) == clean.lower()))
    ).scalar_one_or_none()

    person: Person | None = existing
    actions: list[dict[str, Any]] = []

    # Prefer cluster naming (propagates to all members) before single-face tags.
    for cid in cluster_ids:
        cluster = await session.get(FaceCluster, cid)
        if cluster is None:
            actions.append({"type": "cluster", "id": cid, "ok": False, "error": "Cluster not found"})
            continue
        try:
            if person is None:
                person = await name_cluster(session, cid, clean)
                actions.append({"type": "cluster", "id": cid, "ok": True, "action": "named"})
            else:
                person = await merge_cluster_into_person(session, cid, person.id)
                actions.append({"type": "cluster", "id": cid, "ok": True, "action": "merged"})
        except ValueError as exc:
            actions.append({"type": "cluster", "id": cid, "ok": False, "error": str(exc)})

    for fid in face_ids:
        face = await session.get(Face, fid)
        if face is None:
            actions.append({"type": "face", "id": fid, "ok": False, "error": "Face not found"})
            continue
        # Skip faces already on the target person
        if person is not None and face.person_id == person.id:
            actions.append({"type": "face", "id": fid, "ok": True, "action": "already_linked"})
            continue
        try:
            person = await tag_face_manual(session, fid, clean)
            actions.append({"type": "face", "id": fid, "ok": True, "action": "tagged"})
        except ValueError as exc:
            actions.append({"type": "face", "id": fid, "ok": False, "error": str(exc)})

    if person is None:
        raise ValueError("Could not create or link a person from the provided matches")

    # Soft-set non-student role for leadership when provided and unset
    if role and person.role is None:
        person.role = "non_student"
        await session.flush()

    occ = (
        await session.execute(select(func.count()).select_from(Face).where(Face.person_id == person.id))
    ).scalar_one()

    return {
        "ok": True,
        "person": {
            "id": person.id,
            "name": person.name,
            "role": person.role,
            "representative_face_id": person.representative_face_id,
            "occurrence_count": int(occ),
        },
        "actions": actions,
        "named": clean,
    }
