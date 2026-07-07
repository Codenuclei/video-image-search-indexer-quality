from __future__ import annotations

import re

from sqlalchemy import exists, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import DriveFile, DriveFileStatus, Face, Media, MediaType, Person
from app.gemini.tags import person_names_for_drive_file
from app.schemas import SearchResultFile

_PEOPLE_PATTERN = re.compile(
    r"\b(people|person|persons|human|humans|face|faces|portrait|portraits|selfie|selfies)\b",
    re.IGNORECASE,
)
_SCENE_PATTERN = re.compile(
    r"\b(party|partying|parties|celebration|celebrating|dance|dancing|birthday|"
    r"rooftop|gathering|event|fun|crowd)\b",
    re.IGNORECASE,
)
_ACTION_PATTERN = re.compile(
    r"\b(hold|holding|drink|drinking|smile|smiling|laugh|laughing|hug|hugging|"
    r"sit|sitting|stand|standing|walk|walking|run|running|eat|eating|wear|wearing)\b",
    re.IGNORECASE,
)


def is_people_query(query: str) -> bool:
    return bool(_PEOPLE_PATTERN.search(query))


def is_scene_query(query: str) -> bool:
    return bool(_SCENE_PATTERN.search(query))


def is_image_query(query: str) -> bool:
    return bool(
        re.compile(
            r"\b(image|images|photo|photos|picture|pictures|jpeg|jpg|png|webp)\b",
            re.IGNORECASE,
        ).search(query)
    )


def looks_like_filename(query: str) -> bool:
    q = query.strip()
    return bool(q) and ("." in q and " " not in q)


def is_local_keyword_query(query: str) -> bool:
    """Queries local DB handles well without Gemini."""
    return is_people_query(query) or is_image_query(query)


def _query_tokens(query: str) -> list[str]:
    words = re.findall(r"\w+", query.lower())
    tokens: set[str] = set(words)
    for word in list(tokens):
        if len(word) < 3:
            continue
        if word.endswith("s") and len(word) > 3:
            tokens.add(word[:-1])
        elif not word.endswith("s"):
            tokens.add(f"{word}s")
    return [t for t in tokens if len(t) >= 3]


def needs_strict_relevance_filter(query: str) -> bool:
    """Single-word object queries should not keep unrelated Gemini citations."""
    if is_scene_query(query) or is_people_query(query) or is_image_query(query):
        return False
    if len(query.split()) >= 2:
        return False
    if _ACTION_PATTERN.search(query):
        return False
    return True


def text_matches_query(*parts: str | None, query: str) -> bool:
    haystack = " ".join(p for p in parts if p).lower()
    if not haystack:
        return False
    return any(token in haystack for token in _query_tokens(query))


def has_strong_filename_match(query: str, files: list[SearchResultFile]) -> bool:
    for item in files:
        if text_matches_query(item.name, item.path, query=query):
            return True
    return False


def needs_semantic_search(query: str, person_name: str | None, local_count: int) -> bool:
    """
    Use Gemini File Search for visual/semantic matching: objects, actions, poses,
    expressions, scenes — not just hard-coded scene keywords.
    """
    if person_name and person_name.strip():
        return True
    if is_scene_query(query):
        return True
    if looks_like_filename(query):
        return local_count == 0
    if is_local_keyword_query(query) and local_count > 0:
        return False
    # Object/action/pose/expression queries like "wine glass", "smiling", "dancing"
    return True


def expand_visual_query(query: str) -> list[str]:
    """Add focused variants so object/action searches hit more relevant indexed photos."""
    q = query.strip()
    if not q:
        return []
    variants = [q]
    lower = q.lower()
    if "wine" in lower or "glass" in lower:
        for extra in ("wine glass", "holding wine glass", "person drinking at party"):
            if extra.lower() != lower:
                variants.append(extra)
    if "hold" in lower and "glass" in lower and "wine glass" not in lower:
        variants.append("wine glass")
    if is_scene_query(q):
        for extra in ("celebration", "gathering", "event", "people at party"):
            if extra.lower() not in lower:
                variants.append(extra)
    # preserve order, cap to avoid slow multi-search
    seen: set[str] = set()
    ordered: list[str] = []
    for item in variants:
        key = item.lower()
        if key not in seen:
            seen.add(key)
            ordered.append(item)
    return ordered[:3]


def filter_by_mime(files: list[SearchResultFile], mime_filter: str | None) -> list[SearchResultFile]:
    if not mime_filter or mime_filter == "all":
        return files
    if mime_filter == "image":
        return [f for f in files if f.mime_type.startswith("image/")]
    if mime_filter == "pdf":
        return [f for f in files if f.mime_type == "application/pdf"]
    if mime_filter == "video":
        return [f for f in files if f.mime_type.startswith("video/")]
    return files


def normalize_visual_query(query: str) -> str:
    q = re.sub(r"\bin\s+a\b", "", query, flags=re.IGNORECASE)
    q = re.sub(r"\s+", " ", q).strip()
    return q


def find_person_names_in_query(query: str, known_names: list[str]) -> list[str]:
    matches: list[str] = []
    for name in sorted(known_names, key=len, reverse=True):
        if re.search(rf"\b{re.escape(name)}\b", query, flags=re.IGNORECASE):
            matches.append(name)
    return matches


def strip_person_names(query: str, names: list[str]) -> str:
    result = query
    for name in names:
        result = re.sub(rf"\b{re.escape(name)}\b", "", result, flags=re.IGNORECASE)
    return normalize_visual_query(result)


async def resolve_search_context(
    session: AsyncSession,
    query: str,
    person_param: str | None,
) -> tuple[str | None, str]:
    """Resolve effective person filter and the visual/scene part of the query."""
    explicit = person_param.strip() if person_param and person_param.strip() else None
    if explicit:
        visual = strip_person_names(query, [explicit])
        return explicit, visual or query

    known = list(
        (await session.execute(select(Person.name).order_by(Person.name))).scalars().all()
    )
    matched = find_person_names_in_query(query, known)
    if not matched:
        return None, query

    effective = matched[0]
    visual = strip_person_names(query, matched)
    return effective, visual or query


def has_scene_or_visual_constraint(query: str) -> bool:
    q = query.strip()
    if not q:
        return False
    return is_scene_query(q) or is_people_query(q) or len(q.split()) >= 2 or bool(_ACTION_PATTERN.search(q))


def intersect_person_and_scene(
    local_files: list[SearchResultFile],
    gemini_files: list[SearchResultFile],
    *,
    effective_person: str | None,
    visual_query: str,
) -> list[SearchResultFile]:
    if not effective_person:
        return merge_files(local_files, gemini_files)

    if not has_scene_or_visual_constraint(visual_query):
        return local_files if local_files else gemini_files

    if not gemini_files:
        return local_files

    if not local_files:
        return gemini_files

    gemini_ids = {item.drive_file_id for item in gemini_files}
    matched = [item for item in local_files if item.drive_file_id in gemini_ids]
    return matched if matched else local_files


def normalize_visual_query(query: str) -> str:
    q = re.sub(r"\bin\s+a\b", "", query, flags=re.IGNORECASE)
    q = re.sub(r"\s+", " ", q).strip(" ,")
    return q


def find_person_names_in_query(query: str, known_names: list[str]) -> list[str]:
    matches: list[str] = []
    for name in sorted(known_names, key=len, reverse=True):
        if re.search(rf"\b{re.escape(name)}\b", query, flags=re.IGNORECASE):
            matches.append(name)
    return matches


def strip_person_names(query: str, names: list[str]) -> str:
    result = query
    for name in names:
        result = re.sub(rf"\b{re.escape(name)}\b", "", result, flags=re.IGNORECASE)
    return normalize_visual_query(result)


async def known_person_names(session: AsyncSession) -> list[str]:
    return list((await session.execute(select(Person.name).order_by(Person.name))).scalars().all())


async def resolve_search_context(
    session: AsyncSession,
    query: str,
    person_param: str | None,
) -> tuple[str | None, str]:
    """Resolve effective person filter and the visual part of the query."""
    known = await known_person_names(session)
    if person_param and person_param.strip():
        person = person_param.strip()
        for known_name in known:
            if known_name.lower() == person.lower():
                person = known_name
                break
        visual = strip_person_names(query, [person])
        return person, visual or query

    matched = find_person_names_in_query(query, known)
    if matched:
        if len(matched) == 1 and is_scene_query(query) and query.strip().lower() == matched[0].lower():
            return None, query

        primary = matched[0]
        for name in matched:
            if not is_scene_query(name):
                primary = name
                break
        visual = strip_person_names(query, [primary])
        return primary, visual or query

    return None, query


def is_person_constrained_query(person_name: str | None, visual_query: str, original_query: str) -> bool:
    if not person_name:
        return False
    if is_scene_query(visual_query) or is_scene_query(original_query):
        return True
    if visual_query.strip().lower() != person_name.strip().lower() and visual_query.strip():
        return True
    return False


def merge_person_scene_results(
    local_files: list[SearchResultFile],
    gemini_files: list[SearchResultFile],
    *,
    person_name: str | None,
    visual_query: str,
    original_query: str,
) -> list[SearchResultFile]:
    if not person_name:
        return merge_files(local_files, gemini_files)

    if not gemini_files:
        return local_files

    if is_person_constrained_query(person_name, visual_query, original_query):
        local_ids = {item.drive_file_id for item in local_files}
        scene_hits = [item for item in gemini_files if item.drive_file_id in local_ids]
        return scene_hits or local_files

    return merge_files(local_files, gemini_files)


def filter_to_tagged_person(
    files: list[SearchResultFile],
    person_name: str,
) -> list[SearchResultFile]:
    target = person_name.strip().lower()
    return [item for item in files if any(name.lower() == target for name in item.person_names)]


async def find_matching_files(
    session: AsyncSession,
    query: str,
    person_name: str | None = None,
) -> list[SearchResultFile]:
    """Return indexed Drive files matching the query from the local database."""
    stmt = select(DriveFile).join(Media, Media.drive_file_id == DriveFile.id, isouter=True)

    if person_name and person_name.strip():
        stmt = stmt.where(
            DriveFile.status.in_([DriveFileStatus.PROCESSED, DriveFileStatus.ERROR])
        )
        name = person_name.strip()
        stmt = stmt.where(
            exists(
                select(1)
                .select_from(Face)
                .join(Media, Media.id == Face.media_id)
                .join(Person, Person.id == Face.person_id)
                .where(Media.drive_file_id == DriveFile.id)
                .where(Person.name.ilike(name))
            )
        )
    else:
        stmt = stmt.where(DriveFile.status == DriveFileStatus.PROCESSED)
        if is_people_query(query) or is_image_query(query):
            stmt = stmt.where(DriveFile.mime_type.like("image/%"))
        elif query.strip():
            pattern = f"%{query.strip()}%"
            stmt = stmt.where(or_(DriveFile.name.ilike(pattern), DriveFile.path.ilike(pattern)))

    stmt = stmt.distinct().order_by(DriveFile.path).limit(100)
    drive_files = list((await session.execute(stmt)).scalars().all())

    results: list[SearchResultFile] = []
    for drive_file in drive_files:
        names = await person_names_for_drive_file(session, drive_file.id)
        results.append(
            SearchResultFile(
                drive_file_id=drive_file.id,
                name=drive_file.name,
                path=drive_file.path,
                mime_type=drive_file.mime_type,
                person_names=names,
            )
        )
    return results


async def files_for_citation_names(
    session: AsyncSession,
    file_names: list[str],
) -> list[SearchResultFile]:
    """Match Gemini citation file names back to Drive files."""
    if not file_names:
        return []
    names = [n.strip() for n in file_names if n and n.strip()]
    if not names:
        return []

    conditions = [DriveFile.name.ilike(f"%{name}%") for name in names]
    stmt = (
        select(DriveFile)
        .where(DriveFile.status == DriveFileStatus.PROCESSED)
        .where(or_(*conditions))
        .limit(50)
    )
    drive_files = list((await session.execute(stmt)).scalars().all())
    results: list[SearchResultFile] = []
    for drive_file in drive_files:
        person_names = await person_names_for_drive_file(session, drive_file.id)
        results.append(
            SearchResultFile(
                drive_file_id=drive_file.id,
                name=drive_file.name,
                path=drive_file.path,
                mime_type=drive_file.mime_type,
                person_names=person_names,
            )
        )
    return results


def merge_files(*groups: list[SearchResultFile]) -> list[SearchResultFile]:
    merged: dict[str, SearchResultFile] = {}
    for group in groups:
        for item in group:
            existing = merged.get(item.drive_file_id)
            if existing is None:
                merged[item.drive_file_id] = item
                continue
            names = sorted(set(existing.person_names) | set(item.person_names))
            merged[item.drive_file_id] = existing.model_copy(update={"person_names": names})
    return sorted(merged.values(), key=lambda f: f.path.lower())


async def files_for_drive_ids(
    session: AsyncSession,
    drive_file_ids: list[str],
) -> list[SearchResultFile]:
    if not drive_file_ids:
        return []

    stmt = select(DriveFile).where(DriveFile.id.in_(drive_file_ids))
    drive_files = list((await session.execute(stmt)).scalars().all())

    results: list[SearchResultFile] = []
    for drive_file in drive_files:
        names = await person_names_for_drive_file(session, drive_file.id)
        results.append(
            SearchResultFile(
                drive_file_id=drive_file.id,
                name=drive_file.name,
                path=drive_file.path,
                mime_type=drive_file.mime_type,
                person_names=names,
            )
        )
    return results
