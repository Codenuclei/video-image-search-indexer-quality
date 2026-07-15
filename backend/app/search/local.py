from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass

from sqlalchemy import exists, func, or_, select
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
    r"\b(hold|holding|hand|handing|giv|giving|drink|drinking|smile|smiling|laugh|laughing|hug|hugging|"
    r"sit|sitting|stand|standing|walk|walking|run|running|eat|eating|wear|wearing|"
    r"talk|talking|speak|speaking|chat|chatting|discuss|discussing|conversation|conversing|"
    r"cook|cooking|chop|chopping|grill|grilling|bake|baking|fry|frying|prepare|preparing|"
    r"dance|dancing|play|playing|study|studying|work|working|present|presenting)\b",
    re.IGNORECASE,
)

_ACTION_WORDS = frozenset({
    "hold", "holding", "drink", "drinking", "smile", "smiling", "laugh", "laughing",
    "hug", "hugging", "sit", "sitting", "stand", "standing", "walk", "walking",
    "run", "running", "eat", "eating", "wear", "wearing",
    "talk", "talking", "speak", "speaking", "chat", "chatting",
    "discuss", "discussing", "conversation", "conversing",
    "cook", "cooking", "chop", "chopping", "grill", "grilling", "bake", "baking",
    "fry", "frying", "prepare", "preparing", "dance", "dancing", "play", "playing",
    "study", "studying", "work", "working", "present", "presenting",
    "with", "and",
})

_ACTION_KEYWORD_GROUPS: dict[str, frozenset[str]] = {
    "cook": frozenset({
        "cook", "cooking", "cooked", "kitchen", "chop", "chopping", "stove", "pan",
        "grill", "grilling", "bake", "baking", "fry", "frying", "chef", "preparing",
        "food prep", "apron", "hairnet", "cutting board", "utensil",
    }),
    "eat": frozenset({"eat", "eating", "meal", "dining", "lunch", "dinner", "breakfast", "food"}),
    "dance": frozenset({"dance", "dancing", "dancer"}),
    "talk": frozenset({"talk", "talking", "speak", "speaking", "conversation", "discuss", "discussion"}),
    "study": frozenset({"study", "studying", "reading", "book", "library", "homework"}),
    "work": frozenset({"work", "working", "workshop", "tools", "electronics", "building"}),
    "present": frozenset({"present", "presenting", "presentation", "podium", "stage", "lecture"}),
    "give": frozenset({
        "give", "giving", "hand", "handing", "cheque", "cheques", "check", "checks",
        "award", "prize", "certificate", "ceremonial", "ceremony", "scholarship",
        "grant", "presenting", "presentation", "donation", "oversized",
    }),
}


_PEOPLE_WORDS = frozenset({
    "people", "person", "persons", "human", "humans",
    "face", "faces", "portrait", "portraits", "selfie", "selfies",
})

_CO_OCCUR_WITH_STUDENTS = re.compile(
    r"\b(?:with|and)\s+students?\b|\bstudents?\s+(?:with|and)\b",
    re.IGNORECASE,
)
_NON_STUDENT_WITH_STUDENTS = re.compile(
    r"\b(?:non[- ]?students?|teachers?|faculty|staff)\s+(?:with|and)\s+students?\b",
    re.IGNORECASE,
)
# "giving cheque to students" — students are the recipient, not a face-tag filter
_STUDENT_OBJECT_PHRASE = re.compile(
    r"\b(?:to|for|among|between|from|about)\s+students?\b",
    re.IGNORECASE,
)
_STUDENT_WORD = re.compile(r"\bstudents?\b", re.IGNORECASE)
_NON_STUDENT_WORD = re.compile(
    r"\b(?:non[- ]?students?|teachers?|faculty|staff)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class SearchRoleContext:
    """Role-based filters derived from the query (manual Person.role tags)."""

    co_occur_roles: tuple[str, ...] = ()  # required alongside named person(s)
    require_all_roles: tuple[str, ...] = ()  # file must include every listed role
    student_context: bool = False  # query involves students (to/with/for students, etc.)


def query_has_student_context(query: str) -> bool:
    """True when the query involves students, even as recipients ('cheque to students')."""
    return bool(
        _STUDENT_WORD.search(query)
        or _CO_OCCUR_WITH_STUDENTS.search(query)
        or _STUDENT_OBJECT_PHRASE.search(query)
        or _NON_STUDENT_WITH_STUDENTS.search(query)
    )


def parse_role_context(query: str) -> tuple[str, SearchRoleContext]:
    """Strip role keywords and return remaining text + role filter context."""
    work = query
    co_occur: list[str] = []
    require_all: list[str] = []
    student_context = query_has_student_context(query)

    if _NON_STUDENT_WITH_STUDENTS.search(work):
        require_all.extend(["non_student", "student"])
        work = _NON_STUDENT_WITH_STUDENTS.sub(" ", work)
    elif _CO_OCCUR_WITH_STUDENTS.search(work):
        co_occur.append("student")
        work = _CO_OCCUR_WITH_STUDENTS.sub(" ", work)

    work = _STUDENT_OBJECT_PHRASE.sub(" ", work)

    if (
        re.match(r"^students?\b", work.strip(), re.IGNORECASE)
        and not co_occur
        and "student" not in require_all
    ):
        require_all.append("student")
    work = _STUDENT_WORD.sub(" ", work)

    if _NON_STUDENT_WORD.search(work) and "non_student" not in require_all:
        require_all.append("non_student")
    work = _NON_STUDENT_WORD.sub(" ", work)

    cleaned = normalize_visual_query(work)
    ctx = SearchRoleContext(
        co_occur_roles=tuple(dict.fromkeys(co_occur)),
        require_all_roles=tuple(dict.fromkeys(require_all)),
        student_context=student_context,
    )
    return cleaned, ctx


def role_context_active(ctx: SearchRoleContext) -> bool:
    return bool(ctx.co_occur_roles or ctx.require_all_roles)


def role_context_needs_student(ctx: SearchRoleContext) -> bool:
    return "student" in ctx.co_occur_roles or "student" in ctx.require_all_roles


_STUDENT_CAPTION_RE = re.compile(
    r"\b(?:students?|pupils?|classmates?|young\s+people|college\s+students?|university\s+students?)\b",
    re.IGNORECASE,
)


async def drive_file_ids_with_student_captions(
    session: AsyncSession,
    drive_file_ids: list[str],
) -> list[str]:
    """Files whose indexed captions mention students (fallback when no student face tags)."""
    if not drive_file_ids:
        return []
    from app.qdrant.image_captions import get_captions_by_ids_sync

    captions = await asyncio.to_thread(get_captions_by_ids_sync, drive_file_ids)
    return [fid for fid, text in captions.items() if _STUDENT_CAPTION_RE.search(text or "")]


async def resolve_role_matching_file_ids(
    session: AsyncSession,
    drive_file_ids: list[str],
    *,
    person_names: list[str],
    role_ctx: SearchRoleContext,
) -> list[str]:
    """Face-tag role SQL; caption fallback only when no faces match."""
    if not drive_file_ids or not role_context_active(role_ctx):
        return drive_file_ids

    sql_ids = await matching_drive_file_ids_for_roles(
        session,
        drive_file_ids,
        person_names=person_names,
        role_ctx=role_ctx,
    )
    if sql_ids or not role_context_needs_student(role_ctx):
        return sql_ids

    return await drive_file_ids_with_student_captions(session, drive_file_ids)


def is_people_query(query: str) -> bool:
    """True only for browse-all-people queries, not compound scene queries like 'person handing cheque'."""
    if not _PEOPLE_PATTERN.search(query):
        return False
    tokens = set(re.findall(r"\w+", query.lower()))
    return tokens.issubset(_PEOPLE_WORDS)


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
    q = re.sub(r"\b(with|and|&)\b", "", query, flags=re.IGNORECASE)
    q = re.sub(r"\s+", " ", q).strip(" ,")
    return q


def find_person_names_in_query(query: str, known_names: list[str]) -> list[str]:
    matches: list[str] = []
    matched_starts: set[int] = set()

    for name in sorted(known_names, key=len, reverse=True):
        for hit in re.finditer(rf"\b{re.escape(name)}\b", query, flags=re.IGNORECASE):
            if hit.start() in matched_starts:
                continue
            matches.append(name)
            matched_starts.add(hit.start())
            break

    first_to_names: dict[str, list[str]] = {}
    for name in known_names:
        first = name.split()[0].lower()
        first_to_names.setdefault(first, []).append(name)

    for hit in re.finditer(r"\b(\w+)\b", query):
        if hit.start() in matched_starts:
            continue
        token = hit.group(1).lower()
        candidates = first_to_names.get(token, [])
        if len(candidates) != 1:
            continue
        name = candidates[0]
        if name in matches:
            continue
        matches.append(name)
        matched_starts.add(hit.start())

    return matches


def strip_person_names(query: str, names: list[str]) -> str:
    result = query
    for name in names:
        result = re.sub(rf"\b{re.escape(name)}\b", "", result, flags=re.IGNORECASE)
        first = name.split()[0]
        if first.lower() != name.lower():
            result = re.sub(rf"\b{re.escape(first)}\b", "", result, flags=re.IGNORECASE)
    return normalize_visual_query(result)


def is_action_only_query(query: str) -> bool:
    tokens = set(re.findall(r"\w+", query.lower()))
    if not tokens:
        return True
    return tokens.issubset(_ACTION_WORDS)


def is_action_query(query: str) -> bool:
    """True when the query names a specific action (cooking, dancing, etc.)."""
    return bool(_ACTION_PATTERN.search(query))


def action_match_keywords(query: str) -> set[str]:
    """Keywords that indicate the described action is present in a caption."""
    q = query.lower()
    keywords: set[str] = set()
    cook_query = any(
        w in q for w in (
            "cook", "cooking", "chop", "chopping", "grill", "grilling",
            "bake", "baking", "fry", "frying", "prepare", "preparing",
        )
    )
    for stem, group in _ACTION_KEYWORD_GROUPS.items():
        if stem == "eat" and cook_query:
            # "food" in "students cooking food" must not widen to eating/dining captions.
            if not any(w in q for w in ("eat", "eating", "meal", "dining", "lunch", "dinner", "breakfast")):
                continue
        if any(word in q for word in group):
            keywords |= group
    for match in _ACTION_PATTERN.finditer(query):
        word = match.group().lower()
        keywords.add(word)
        if word.endswith("ing") and len(word) > 4:
            keywords.add(word[:-3])
    return keywords


_ACTION_NEGATIVE_HINTS: dict[str, frozenset[str]] = {
    "cook": frozenset({
        "panel discussion", "panel", "lecture", "foosball", "library", "workshop",
        "market", "cafeteria", "eating lunch", "sitting at a table", "dining hall",
        "playing", "audience", "eating", "dining", "meal", "seated", "conversation",
        "ceremony", "cheque", "standing", "portrait", "podium", "presentation",
        "group photo", "smiling at camera", "microphone", "speaking into", "interview",
        "clapperboard", "seminar", "conference hall", "conference", "trophy",
        "instructor", "classroom", "discussion", "celebrates", "tracksuit",
    }),
}


def caption_contradicts_action(caption: str, query: str) -> bool:
    q = query.lower()
    cap = caption.lower()
    for stem, negatives in _ACTION_NEGATIVE_HINTS.items():
        if stem in q:
            return any(neg in cap for neg in negatives)
    return False


def caption_matches_action(caption: str, keywords: set[str]) -> bool:
    if not keywords or not caption.strip():
        return False
    cap = caption.lower()
    return any(kw in cap for kw in keywords)


def merge_action_search_pool(
    image_files: list[SearchResultFile],
    keyword_matched: list[SearchResultFile],
    *,
    max_pool: int = 80,
) -> list[SearchResultFile]:
    """Keyword hits first, then other captioned candidates for LLM validation."""
    if not keyword_matched:
        return image_files
    seen: set[str] = set()
    pool: list[SearchResultFile] = []
    for item in keyword_matched:
        if item.drive_file_id in seen:
            continue
        seen.add(item.drive_file_id)
        pool.append(item)
    for item in sorted(image_files, key=lambda f: (-(f.score or 0.0), f.name.lower())):
        if item.drive_file_id in seen:
            continue
        if not (item.caption or "").strip():
            continue
        seen.add(item.drive_file_id)
        pool.append(item)
        if len(pool) >= max_pool:
            break
    return pool


def build_strict_action_pool(
    image_files: list[SearchResultFile],
    keyword_matched: list[SearchResultFile],
    keywords: set[str],
    query: str,
    *,
    max_pool: int = 30,
    max_extra: int = 12,
) -> list[SearchResultFile]:
    """Action-only queries: keyword hits + captions that mention the action (no student flood)."""
    if keyword_matched:
        seen = {f.drive_file_id for f in keyword_matched}
        extra = [
            f for f in image_files
            if f.drive_file_id not in seen
            and f.caption
            and caption_matches_action(f.caption, keywords)
            and not caption_contradicts_action(f.caption, query)
        ]
        extra.sort(key=lambda f: (-(f.score or 0.0), f.name.lower()))
        pool = list(keyword_matched) + extra[:max_extra]
        return pool[:max_pool]

    strict = [
        f for f in image_files
        if f.caption
        and caption_matches_action(f.caption, keywords)
        and not caption_contradicts_action(f.caption, query)
    ]
    strict.sort(key=lambda f: (-(f.score or 0.0), f.name.lower()))
    return strict[:max_pool]


def finalize_action_search_results(
    results: list[SearchResultFile],
    keyword_matched: list[SearchResultFile],
    *,
    max_results: int = 12,
) -> list[SearchResultFile]:
    """Keyword-confirmed hits first; cap total so tail trash is dropped."""
    if not results:
        return keyword_matched[:max_results]

    kw_ids = {f.drive_file_id for f in keyword_matched}
    primary = [f for f in keyword_matched if f.drive_file_id in {r.drive_file_id for r in results}]
    secondary = [f for f in results if f.drive_file_id not in kw_ids]

    seen: set[str] = set()
    ordered: list[SearchResultFile] = []
    for item in primary + secondary:
        if item.drive_file_id in seen:
            continue
        seen.add(item.drive_file_id)
        ordered.append(item)
        if len(ordered) >= max_results:
            break
    return ordered


def dedupe_search_files(files: list[SearchResultFile]) -> list[SearchResultFile]:
    """Drop duplicate filenames (same asset synced in multiple folders)."""
    seen: set[str] = set()
    out: list[SearchResultFile] = []
    for item in files:
        key = item.name.strip().lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


def is_weak_person_visual(visual_query: str, person_names: list[str]) -> bool:
    """True when the query is mostly about who is in the photo, not a scene description."""
    visual = visual_query.strip().lower()
    if not visual:
        return True
    if visual in ("with", "and", "&"):
        return True
    if is_action_only_query(visual):
        return True
    person_tokens = {part.lower() for name in person_names for part in name.split()}
    leftover = set(re.findall(r"\w+", visual))
    return bool(leftover) and leftover.issubset(person_tokens | _ACTION_WORDS)


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


async def known_person_names(session: AsyncSession) -> list[str]:
    return list((await session.execute(select(Person.name).order_by(Person.name))).scalars().all())


async def resolve_search_context(
    session: AsyncSession,
    query: str,
    person_param: str | None,
) -> tuple[list[str], str, SearchRoleContext]:
    """Resolve person filters, role filters, and remaining visual/scene text."""
    cleaned_query, role_ctx = parse_role_context(query)
    known = await known_person_names(session)
    if person_param and person_param.strip():
        person = person_param.strip()
        for known_name in known:
            if known_name.lower() == person.lower():
                person = known_name
                break
        visual = strip_person_names(cleaned_query, [person])
        return [person], visual or cleaned_query or query, role_ctx

    matched = find_person_names_in_query(cleaned_query, known)
    if not matched:
        return [], cleaned_query or query, role_ctx

    if len(matched) == 1 and is_scene_query(query) and query.strip().lower() == matched[0].lower():
        return [], query, role_ctx

    visual = strip_person_names(cleaned_query, matched)
    return matched, visual or cleaned_query or query, role_ctx


def _person_face_exists_clause(person_name: str):
    return exists(
        select(1)
        .select_from(Face)
        .join(Media, Media.id == Face.media_id)
        .join(Person, Person.id == Face.person_id)
        .where(Media.drive_file_id == DriveFile.id)
        .where(Person.name.ilike(person_name))
    )


def _min_faces_in_file_clause(*, min_faces: int = 2):
    return exists(
        select(1)
        .select_from(Media)
        .join(Face, Face.media_id == Media.id)
        .where(Media.drive_file_id == DriveFile.id)
        .group_by(Media.drive_file_id)
        .having(func.count(Face.id) >= min_faces)
    )


async def non_student_names_among(
    session: AsyncSession,
    person_names: list[str],
) -> list[str]:
    if not person_names:
        return []
    lowered = {n.strip().lower() for n in person_names if n.strip()}
    rows = (
        await session.execute(
            select(Person.name).where(Person.role == "non_student")
        )
    ).all()
    return [name for (name,) in rows if name.strip().lower() in lowered]


async def drive_file_ids_with_person_and_companion(
    session: AsyncSession,
    drive_file_ids: list[str],
    *,
    person_names: list[str],
    min_faces: int = 2,
) -> list[str]:
    """Keep files where named person appears with at least one other detected face."""
    if not drive_file_ids or not person_names:
        return drive_file_ids

    stmt = select(DriveFile.id).where(DriveFile.id.in_(drive_file_ids))
    for name in person_names:
        stmt = stmt.where(_person_face_exists_clause(name))
    stmt = stmt.where(_min_faces_in_file_clause(min_faces=min_faces))
    return list((await session.execute(stmt)).scalars().all())


async def non_student_person_ids_for_names(
    session: AsyncSession,
    person_names: list[str],
) -> list[int]:
    names = await non_student_names_among(session, person_names)
    if not names:
        return []
    rows = (
        await session.execute(
            select(Person.id).where(
                or_(*[Person.name.ilike(name) for name in names])
            )
        )
    ).all()
    return [row[0] for row in rows]


def _additional_person_face_clause(
    exclude_person_ids: list[int],
    *,
    min_confidence: float = 0.72,
):
    """Another person in frame — not the same tagged non-student."""
    return exists(
        select(1)
        .select_from(Face)
        .join(Media, Media.id == Face.media_id)
        .where(Media.drive_file_id == DriveFile.id)
        .where(Face.detection_confidence >= min_confidence)
        .where(
            or_(
                Face.person_id.is_(None),
                ~Face.person_id.in_(exclude_person_ids),
            )
        )
    )


async def drive_file_ids_with_non_student_and_companion(
    session: AsyncSession,
    drive_file_ids: list[str],
    *,
    person_names: list[str],
) -> list[str]:
    exclude_ids = await non_student_person_ids_for_names(session, person_names)
    if not exclude_ids or not drive_file_ids:
        return []

    stmt = select(DriveFile.id).where(DriveFile.id.in_(drive_file_ids))
    for name in person_names:
        stmt = stmt.where(_person_face_exists_clause(name))
    stmt = stmt.where(_additional_person_face_clause(exclude_ids))
    return list((await session.execute(stmt)).scalars().all())


async def filter_non_student_solo_in_student_context(
    session: AsyncSession,
    files: list[SearchResultFile],
    *,
    person_names: list[str],
    role_ctx: SearchRoleContext,
) -> list[SearchResultFile]:
    """Non-student + student-context queries must include another person in frame."""
    if not role_ctx.student_context or not person_names or not files:
        return files
    if not await non_student_names_among(session, person_names):
        return files

    valid = set(
        await drive_file_ids_with_non_student_and_companion(
            session,
            [item.drive_file_id for item in files],
            person_names=person_names,
        )
    )
    return [item for item in files if item.drive_file_id in valid]


def _student_face_exists_clause():
    """Default student: unnamed faces or anyone not explicitly marked non_student."""
    return exists(
        select(1)
        .select_from(Face)
        .join(Media, Media.id == Face.media_id)
        .outerjoin(Person, Person.id == Face.person_id)
        .where(Media.drive_file_id == DriveFile.id)
        .where(
            or_(
                Face.person_id.is_(None),
                Person.role.is_(None),
                Person.role == "student",
            )
        )
    )


def _non_student_face_exists_clause():
    return exists(
        select(1)
        .select_from(Face)
        .join(Media, Media.id == Face.media_id)
        .join(Person, Person.id == Face.person_id)
        .where(Media.drive_file_id == DriveFile.id)
        .where(Person.role == "non_student")
    )


def _role_face_exists_clause(role: str):
    if role == "student":
        return _student_face_exists_clause()
    if role == "non_student":
        return _non_student_face_exists_clause()
    return exists(
        select(1)
        .select_from(Face)
        .join(Media, Media.id == Face.media_id)
        .join(Person, Person.id == Face.person_id)
        .where(Media.drive_file_id == DriveFile.id)
        .where(Person.role == role)
    )


async def matching_drive_file_ids_for_roles(
    session: AsyncSession,
    drive_file_ids: list[str],
    *,
    person_names: list[str],
    role_ctx: SearchRoleContext,
) -> list[str]:
    """Keep files that satisfy named-person + role co-occurrence constraints."""
    if not drive_file_ids:
        return []
    if not person_names and not role_context_active(role_ctx):
        return drive_file_ids

    stmt = select(DriveFile.id).where(DriveFile.id.in_(drive_file_ids))
    for name in person_names:
        stmt = stmt.where(_person_face_exists_clause(name))
    for role in role_ctx.co_occur_roles:
        stmt = stmt.where(_role_face_exists_clause(role))
    for role in role_ctx.require_all_roles:
        stmt = stmt.where(_role_face_exists_clause(role))
    if role_ctx.student_context and person_names:
        exclude_ids = await non_student_person_ids_for_names(session, person_names)
        if exclude_ids:
            stmt = stmt.where(_additional_person_face_clause(exclude_ids))
    return list((await session.execute(stmt)).scalars().all())


async def filter_files_by_role_context(
    session: AsyncSession,
    files: list[SearchResultFile],
    *,
    person_names: list[str],
    role_ctx: SearchRoleContext,
) -> list[SearchResultFile]:
    if not role_context_active(role_ctx):
        return files
    valid = set(
        await resolve_role_matching_file_ids(
            session,
            [item.drive_file_id for item in files],
            person_names=person_names,
            role_ctx=role_ctx,
        )
    )
    return [item for item in files if item.drive_file_id in valid]


def is_person_constrained_query(person_names: list[str], visual_query: str, original_query: str) -> bool:
    if not person_names:
        return False
    if len(person_names) >= 2:
        return True
    person_name = person_names[0]
    if is_scene_query(visual_query) or is_scene_query(original_query):
        return True
    if visual_query.strip().lower() != person_name.strip().lower() and visual_query.strip():
        return True
    return False


def merge_person_scene_results(
    local_files: list[SearchResultFile],
    gemini_files: list[SearchResultFile],
    *,
    person_names: list[str],
    visual_query: str,
    original_query: str,
) -> list[SearchResultFile]:
    if not person_names:
        return merge_files(local_files, gemini_files)

    if not gemini_files:
        return local_files

    if is_person_constrained_query(person_names, visual_query, original_query):
        local_ids = {item.drive_file_id for item in local_files}
        scene_hits = [item for item in gemini_files if item.drive_file_id in local_ids]
        return scene_hits or local_files

    return merge_files(local_files, gemini_files)


def filter_to_tagged_persons(
    files: list[SearchResultFile],
    person_names: list[str],
) -> list[SearchResultFile]:
    """Keep files where queried persons are face-tagged (all required for multi-person)."""
    if not person_names:
        return files
    required = {name.strip().lower() for name in person_names}
    tagged_ok = (
        (lambda names: required.issubset({name.lower() for name in names}))
        if len(required) >= 2
        else (lambda names: bool(required & {name.lower() for name in names}))
    )
    return [item for item in files if tagged_ok(item.person_names)]


def sort_by_person_overlap(
    files: list[SearchResultFile],
    person_names: list[str],
) -> list[SearchResultFile]:
    """Photos with more queried persons tagged rank higher; all-person matches first."""
    if len(person_names) < 2:
        return files
    required = {name.strip().lower() for name in person_names}

    def sort_key(item: SearchResultFile) -> tuple:
        tagged = {name.lower() for name in item.person_names}
        overlap = len(required & tagged)
        all_match = overlap == len(required)
        return (0 if all_match else 1, -overlap, -(item.score or 0))

    return sorted(files, key=sort_key)


def boost_multi_person_scores(
    files: list[SearchResultFile],
    person_names: list[str],
) -> list[SearchResultFile]:
    """Lift scores for photos that have every queried person face-tagged."""
    if len(person_names) < 2:
        return files
    required = {name.strip().lower() for name in person_names}
    boosted: list[SearchResultFile] = []
    for item in files:
        tagged = {name.lower() for name in item.person_names}
        if not required.issubset(tagged):
            continue
        base = item.score if item.score is not None else 0.55
        boosted.append(item.model_copy(update={"score": min(1.0, round(base + 0.15, 4))}))
    return boosted


def filter_to_tagged_person(
    files: list[SearchResultFile],
    person_name: str,
) -> list[SearchResultFile]:
    return filter_to_tagged_persons(files, [person_name])


async def find_files_by_role_context(
    session: AsyncSession,
    role_ctx: SearchRoleContext,
    *,
    person_names: list[str] | None = None,
) -> list[SearchResultFile]:
    """Files where manually tagged roles (and optional named persons) co-occur."""
    if not role_context_active(role_ctx):
        return []

    names = [n.strip() for n in (person_names or []) if n.strip()]
    stmt = (
        select(DriveFile)
        .join(Media, Media.drive_file_id == DriveFile.id, isouter=True)
        .where(DriveFile.status == DriveFileStatus.PROCESSED)
        .where(DriveFile.mime_type.like("image/%"))
    )
    for name in names:
        stmt = stmt.where(_person_face_exists_clause(name))
    for role in role_ctx.co_occur_roles:
        stmt = stmt.where(_role_face_exists_clause(role))
    for role in role_ctx.require_all_roles:
        stmt = stmt.where(_role_face_exists_clause(role))

    stmt = stmt.distinct().order_by(DriveFile.path).limit(100)
    drive_files = list((await session.execute(stmt)).scalars().all())
    if not drive_files and role_context_needs_student(role_ctx):
        candidates = list(
            (await session.execute(
                select(DriveFile)
                .where(DriveFile.status == DriveFileStatus.PROCESSED)
                .where(DriveFile.mime_type.like("image/%"))
                .order_by(DriveFile.path)
            )).scalars().all()
        )
        if names:
            candidates = [df for df in candidates if df.id in set(
                await matching_drive_file_ids_for_roles(
                    session, [df.id for df in candidates], person_names=names, role_ctx=SearchRoleContext()
                )
            )]
        valid_ids = set(
            await resolve_role_matching_file_ids(
                session,
                [df.id for df in candidates],
                person_names=names,
                role_ctx=role_ctx,
            )
        )
        drive_files = [df for df in candidates if df.id in valid_ids][:100]

    results: list[SearchResultFile] = []
    for drive_file in drive_files:
        tagged = await person_names_for_drive_file(session, drive_file.id)
        results.append(
            SearchResultFile(
                drive_file_id=drive_file.id,
                name=drive_file.name,
                path=drive_file.path,
                mime_type=drive_file.mime_type,
                person_names=tagged,
            )
        )
    return results


async def find_matching_files(
    session: AsyncSession,
    query: str,
    person_names: list[str] | None = None,
    *,
    person_name: str | None = None,
) -> list[SearchResultFile]:
    """Return indexed Drive files matching the query from the local database."""
    names = [n.strip() for n in (person_names or []) if n.strip()]
    if not names and person_name and person_name.strip():
        names = [person_name.strip()]

    stmt = select(DriveFile).join(Media, Media.drive_file_id == DriveFile.id, isouter=True)

    if names:
        stmt = stmt.where(
            DriveFile.status.in_([DriveFileStatus.PROCESSED, DriveFileStatus.ERROR])
        )
        for name in names:
            stmt = stmt.where(_person_face_exists_clause(name))
    else:
        stmt = stmt.where(DriveFile.status == DriveFileStatus.PROCESSED)
        if is_people_query(query) or is_image_query(query):
            stmt = stmt.where(DriveFile.mime_type.like("image/%"))
        elif query.strip():
            pattern = f"%{query.strip()}%"
            stmt = stmt.where(or_(DriveFile.name.ilike(pattern), DriveFile.path.ilike(pattern)))

    stmt = stmt.distinct().order_by(DriveFile.path)
    stmt = stmt.limit(500 if names else 100)
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
