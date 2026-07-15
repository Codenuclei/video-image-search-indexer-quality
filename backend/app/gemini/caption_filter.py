"""Caption-only Gemini filter for image search (text in, booleans out).

Processes the full candidate pool in small batches. Fail-closed on parse errors.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re

from app.schemas import SearchResultFile
from app.search.local import SearchRoleContext, is_action_query

logger = logging.getLogger(__name__)


def _build_prompt(
    query: str,
    visual_query: str,
    n_items: int,
    *,
    person_names: list[str],
    role_ctx: SearchRoleContext | None,
    folder_context: str | None,
    strict_action: bool,
) -> str:
    scene = (visual_query or query).strip()
    lines = [
        "You are a strict image search caption validator.",
        "",
        f'User search query: "{query}"',
    ]
    if scene and scene.lower() != query.strip().lower():
        lines.append(f'Scene/action to match: "{scene}"')
    if person_names:
        names = ", ".join(f'"{n}"' for n in person_names)
        lines.append(
            f"Named person(s) already verified by face recognition (not required in caption text): {names}"
        )
    if role_ctx and role_ctx.student_context:
        if strict_action:
            lines.append(
                "Student + action query: the caption must describe students (or a group of young adults "
                "clearly in a student setting) AND the SPECIFIC action in the query (e.g. actively cooking "
                "in a kitchen — not merely eating, sitting in a classroom, attending a seminar, interview, "
                "or ceremony)."
            )
        else:
            lines.append(
                "Student context: the caption must describe students present in the scene "
                "(group of students, classroom, ceremony with students, cheque to students, etc.). "
                "Reject solo portraits, one-on-one interviews, or adult-only panels with no students mentioned."
            )
    if role_ctx and role_ctx.co_occur_roles:
        lines.append(f"Role co-occurrence required: {', '.join(role_ctx.co_occur_roles)}")
    if role_ctx and role_ctx.require_all_roles:
        lines.append(f"All roles required in scene: {', '.join(role_ctx.require_all_roles)}")
    if folder_context:
        lines.append(f"Folder context: {folder_context}")
    lines += [
        "",
        f"You will see {n_items} image caption(s) (Caption 1 … Caption {n_items}).",
        "",
        "For EACH caption decide:",
    ]
    if strict_action:
        lines += [
            "  true  — the caption shows the SPECIFIC action/scene in the query.",
            "  false — different activity, only a loose topic match, or missing the action.",
            "Be STRICT on the action. When uncertain, return false.",
        ]
    else:
        lines += [
            "  true  — the caption matches the scene/action/role requirements in the query.",
            "  false — wrong activity, missing students when required, or clearly unrelated.",
            "Do NOT require person names to appear in the caption — faces are tagged separately.",
            "When uncertain, return false.",
        ]
    lines += [
        "",
        "Reply ONLY with a JSON array of booleans, one per caption, in order.",
        "Example: [true, false, true]",
    ]
    return "\n".join(lines)


def _parse_booleans(text: str, expected: int) -> list[bool] | None:
    for match in re.finditer(r"\[[\s\S]*?\]", text):
        try:
            arr = json.loads(match.group())
            if isinstance(arr, list) and len(arr) == expected:
                return [bool(v) for v in arr]
        except json.JSONDecodeError:
            continue
    return None


def _filter_batch_sync(
    query: str,
    visual_query: str,
    files: list[SearchResultFile],
    *,
    person_names: list[str],
    role_ctx: SearchRoleContext | None,
    folder_context: str | None,
    strict_action: bool,
    model: str,
    api_key: str,
) -> list[bool] | None:
    from google import genai
    from google.genai import types

    from app.gemini.rate_limit import gemini_vlm_slot, retry_on_rate_limit

    if not files:
        return []

    client = genai.Client(api_key=api_key)
    lines = [
        _build_prompt(
            query,
            visual_query,
            len(files),
            person_names=person_names,
            role_ctx=role_ctx,
            folder_context=folder_context,
            strict_action=strict_action,
        ),
        "",
    ]
    for i, item in enumerate(files, start=1):
        cap = (item.caption or "").strip()
        if not cap:
            lines.append(f"Caption {i} ({item.name}): [MISSING]")
        else:
            lines.append(f'Caption {i} ({item.name}): "{cap}"')

    def _call() -> list[bool]:
        with gemini_vlm_slot():
            response = client.models.generate_content(
                model=model,
                contents=[types.Content(role="user", parts=[types.Part(text="\n".join(lines))])],
                config=types.GenerateContentConfig(
                    temperature=0.0,
                    response_mime_type="application/json",
                ),
            )
        text = response.text or ""
        parsed = _parse_booleans(text, len(files))
        if parsed is not None:
            kept = sum(parsed)
            logger.info(
                "Caption filter batch: %d/%d kept for query %r",
                kept,
                len(files),
                query,
            )
            return parsed
        raise ValueError(f"could not parse boolean array from model: {text[:240]!r}")

    try:
        return retry_on_rate_limit(_call)
    except Exception as exc:
        logger.warning("Caption filter batch failed (%s) — will retry smaller", exc)
        return None


async def _filter_batch_with_split(
    query: str,
    visual_query: str,
    batch: list[SearchResultFile],
    *,
    person_names: list[str],
    role_ctx: SearchRoleContext | None,
    folder_context: str | None,
    strict_action: bool,
    model: str,
    api_key: str,
) -> list[SearchResultFile]:
    mask = await asyncio.to_thread(
        _filter_batch_sync,
        query,
        visual_query,
        batch,
        person_names=person_names,
        role_ctx=role_ctx,
        folder_context=folder_context,
        strict_action=strict_action,
        model=model,
        api_key=api_key,
    )
    if mask is not None:
        return [item for item, ok in zip(batch, mask) if ok]

    if len(batch) <= 1:
        return []

    mid = len(batch) // 2
    left, right = await asyncio.gather(
        _filter_batch_with_split(
            query,
            visual_query,
            batch[:mid],
            person_names=person_names,
            role_ctx=role_ctx,
            folder_context=folder_context,
            strict_action=strict_action,
            model=model,
            api_key=api_key,
        ),
        _filter_batch_with_split(
            query,
            visual_query,
            batch[mid:],
            person_names=person_names,
            role_ctx=role_ctx,
            folder_context=folder_context,
            strict_action=strict_action,
            model=model,
            api_key=api_key,
        ),
    )
    return left + right


async def filter_images_by_caption_llm(
    query: str,
    files: list[SearchResultFile],
    *,
    visual_query: str = "",
    person_names: list[str] | None = None,
    role_ctx: SearchRoleContext | None = None,
    folder_context: str | None = None,
    strict_action: bool | None = None,
) -> list[SearchResultFile]:
    """Filter image hits using caption text only (parallel batched Gemini calls)."""
    from app.concurrency.pools import effective_cpu_workers
    from app.config import get_settings

    settings = get_settings()
    if not settings.gemini_api_key or not settings.search_caption_filter_enabled:
        return files

    captioned = [f for f in files if (f.caption or "").strip()]
    if not captioned:
        logger.info("Caption filter: no captioned hits for query %r — returning empty", query)
        return []

    scene = (visual_query or query).strip()
    action_strict = (
        strict_action
        if strict_action is not None
        else is_action_query(scene or query)
    )
    names = person_names or []

    ranked = sorted(
        captioned,
        key=lambda f: (-(f.score or 0.0), f.name.lower()),
    )
    pool_limit = (
        30
        if action_strict and not person_names
        else settings.search_caption_filter_pool_size
    )
    pool = ranked[:pool_limit]
    batch_size = max(1, min(settings.search_caption_filter_batch_size, 25))
    batches = [pool[i : i + batch_size] for i in range(0, len(pool), batch_size)]
    parallel = (
        settings.search_llm_batch_parallel
        if settings.search_llm_batch_parallel > 0
        else min(2, effective_cpu_workers(settings.cpu_thread_pool_size))
    )
    gap = max(0.0, settings.search_caption_filter_gap_seconds)

    sem = asyncio.Semaphore(parallel)

    async def _run_batch(batch: list[SearchResultFile], batch_index: int) -> list[SearchResultFile]:
        if gap > 0 and batch_index >= parallel:
            await asyncio.sleep(gap)
        async with sem:
            return await _filter_batch_with_split(
                query,
                scene,
                batch,
                person_names=names,
                role_ctx=role_ctx,
                folder_context=folder_context,
                strict_action=action_strict,
                model=settings.gemini_model,
                api_key=settings.gemini_api_key,
            )

    batch_results = await asyncio.gather(
        *[_run_batch(batch, i) for i, batch in enumerate(batches)]
    )
    kept: list[SearchResultFile] = []
    seen: set[str] = set()
    for batch_kept in batch_results:
        for item in batch_kept:
            if item.drive_file_id in seen:
                continue
            seen.add(item.drive_file_id)
            kept.append(item)

    logger.info(
        "Caption filter: %d/%d captioned hits kept for query %r",
        len(kept),
        len(pool),
        query,
    )
    if not kept and pool:
        logger.warning(
            "Caption filter removed entire pool (%d) for query %r — returning empty",
            len(pool),
            query,
        )
    return kept
