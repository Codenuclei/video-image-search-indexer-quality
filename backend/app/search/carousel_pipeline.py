"""Carousel video pipeline: themes, hooks/topics extract, intent.

Rules enforced here:
- Contextual integrity: theme boundaries snap to cue starts (never mid-cue).
- Zero repetition: non-overlapping theme ranges; unique hook/topic strings.
- Person filter (when used) is presence-only: themes are never reframed around a person.
- Generation order: cohesive topics → optional subtopics → hooks (one topic at a time).
- Topics are true thematic clusters from the transcript (where the speaker takes a
  direction), not scattered keyword tags. Subtopics nest under a parent when natural.
- Hooks are exact transcript sentences for a singular topic (no rewrite / punch-up).
- Hooks must not reuse another topic's angle.
- Time spans stay aligned to spoken utterances for frames.
- Hooks/topics prefer English: use parallel English cues when present, else translate.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from typing import Any

from app.search.english_text import (
    cues_need_english,
    english_text_for_window,
    is_english_text,
    needs_english,
    prefer_english_cues,
)
from app.search.transcript_topics import (
    compact_transcript,
    fallback_topics_from_cues,
    format_cue_line,
)  # noqa: F401 — format_cue_line used by chunk helpers

THEMES_RUNTIME_VERSION = "themes-v7-fence-tokens"

logger = logging.getLogger(__name__)

_MAX_THEMES = 8
_MAX_HOOKS = 20
_MAX_TOPICS = 14
_MAX_MERGED_HOOKS = 24
_MAX_MERGED_TOPICS = 24
# Per-chunk transcript budget for Gemini (full timed cues; chunk+merge for long talks).
_TOPIC_CHUNK_CHARS = 12_000
_TOPIC_CHUNK_OVERLAP_CUES = 6
THEME_PROMPT_VERSION = "themes-v5-dense-outline-selfcheck"
EXTRACT_PROMPT_VERSION = "extract-v8-topics-only-keep"

# Shared editorial brief for every hook-writing prompt. A hook has four jobs:
# stop the scroll, open a curiosity loop, earn the share, and build the page's
# voice — while staying honest, non-explicit, and never cliched.
_MU_SACRED_ACTION_VERBS = (
    "Built, Shipped, Created, Explored, Experimented, Failed, Raised, Invested, "
    "Launched, Scaled, Closed, Hired, Sold, Funded, Grew, Learned, Pitched, "
    "Deployed, Prototyped, Iterated"
)
_MU_SACRED_VERB_PATTERN = re.compile(
    r"\b(?:build|built|ship(?:ped)?|creat(?:e|ed)|explor(?:e|ed)|"
    r"experiment(?:ed)?|fail(?:ed)?|rais(?:e|ed)|invest(?:ed)?|"
    r"launch(?:ed)?|scal(?:e|ed)|clos(?:e|ed)|hir(?:e|ed)|sold|sell|"
    r"fund(?:ed)?|gr(?:ew|ow)|learn(?:ed|t)?|pitch(?:ed)?|deploy(?:ed)?|"
    r"prototyped|iterat(?:e|ed))\b",
    re.IGNORECASE,
)


def mu_sacred_action_words(text: str) -> list[str]:
    """Return grounded MU action verbs found in a copy/seed line."""
    return [match.group(0) for match in _MU_SACRED_VERB_PATTERN.finditer(text or "")]


def _grounded_mu_action_clause(text: str, max_words: int = 18) -> str:
    """Extract a short source-backed clause containing an MU action verb."""
    clean = " ".join((text or "").split()).strip()
    if not clean:
        return ""
    sentences = [
        part.strip()
        for part in re.split(r"(?<=[.!?])\s+|[;\n]+", clean)
        if part.strip()
    ]
    chosen = next((part for part in sentences if mu_sacred_action_words(part)), "")
    if not chosen:
        return ""
    words = chosen.split()
    if len(words) <= max_words:
        return chosen
    verb_index = next(
        (
            i
            for i, word in enumerate(words)
            if mu_sacred_action_words(word.strip(".,!?;:\"'()[]"))
        ),
        0,
    )
    start = max(0, min(verb_index - 3, len(words) - max_words))
    return " ".join(words[start : start + max_words]).strip()
_HOOK_CRAFT_BRIEF = (
    "WHAT A HOOK MUST DO (all four jobs):\n"
    "1. STOP THE SCROLL — lead with the most startling concrete detail in the spoken "
    "window (a number, a stake, a contrast, an impossible-sounding outcome). The first "
    "3 words must earn attention on a fast feed.\n"
    "2. OPEN A LOOP — tease, never resolve. Set up the exact question the viewer must "
    "stay to the end to answer. Do NOT give away the payoff, the method, the ending, or "
    "the lesson; withhold at least one key element (the who, the how, or the result).\n"
    "3. EARN THE SHARE — phrase it so a viewer wants to send it to a friend or "
    "colleague: bold enough to repeat out loud, true enough to defend when questioned.\n"
    "4. BUILD THE PAGE — write with a confident editorial voice that makes the viewer "
    "want more from this creator, not a one-off clickbait tone.\n"
    "MU-SACRED ACTION VERB (hard style rule for copy):\n"
    f"- Prefer a past-tense MU action verb when the spoken window supports it: {_MU_SACRED_ACTION_VERBS}.\n"
    "- Lead with or center that verb when honest (e.g. \"Built X before Y\", \"Failed twice, then shipped\").\n"
    "- Do NOT invent an action the speaker did not describe. If no sacred verb fits, keep a "
    "grammatical headline without forcing one.\n"
    "HONESTY AND TONE RULES:\n"
    "- Never mislead: the video must fully deliver what the hook promises.\n"
    "- You may dramatize and sharpen the truth (framing, stakes, emphasis) but never "
    "fabricate a fact, number, name, or event absent from the spoken window.\n"
    "- Use only numbers that appear in the spoken window.\n"
    "- Never explicit, crude, or demeaning toward any person or group.\n"
    "- Never boring, generic, or cliched. BANNED: stock openers such as "
    "\"The hidden pattern behind…\", \"What most people miss…\", \"The real reason "
    "why…\", \"You won't believe…\", \"This will blow your mind…\", and any opener "
    "recycled across hooks in the same batch.\n"
    "GRAMMAR (hard rule):\n"
    "- Every hook must be a grammatical English headline: a complete clause, or "
    "\"Number — claim\". Someone should be able to read it aloud without stumbling.\n"
    "- Never use spoken fillers (like, um, you know, kinda) as hook nouns.\n"
    "- Never glue two random adjacent words into \"What X quietly proves\" or "
    "\"Where X actually wins\". X must be a real noun phrase (a number, a named "
    "thing, or a concrete stake).\n"
    "SHAPE EXAMPLE (do not copy): spoken \"two students sold 33 lakh worth of watches "
    "in eight minutes\" → hook \"Sold ₹33 Lakh in 8 Minutes — by Two Students\" (keeps the "
    "startling numbers + MU action verb, withholds what they sold and how).\n"
)

SLIDE_COPY_PROMPT_VERSION = "slides-v4-mu-sacred-verbs-scrim"

# Same jobs as hooks, applied across a swipeable slide sequence.
_SLIDE_CRAFT_BRIEF = (
    "WHAT THE CAROUSEL MUST DO:\n"
    "1. STOP THE SCROLL on an early slide — lead with the most startling concrete "
    "detail (a number, a stake, a contrast). The first 3 words of the cover must "
    "earn attention.\n"
    "2. OPEN A LOOP — tease, never dump the whole payoff on slide 1. The viewer "
    "should need to swipe to the end. Withhold at least one key element (the who, "
    "the how, or the result) until a later slide.\n"
    "3. EARN THE SHARE — lines a viewer would send a colleague: bold enough to "
    "repeat, true enough to defend.\n"
    "4. BUILD THE PAGE — confident editorial voice, not one-off clickbait.\n"
    "MU-SACRED ACTION VERB (hard style rule for slide copy):\n"
    f"- Across the deck, prefer MU past-tense action verbs when the spoken seeds support them: {_MU_SACRED_ACTION_VERBS}.\n"
    "- At least the cover or the payoff slide should use one when honest "
    "(e.g. Built / Shipped / Created / Failed / Raised / Invested).\n"
    "- Highlight the action verb in yellow when present.\n"
    "- Never invent an action absent from the spoken seeds; skip the verb rather than fabricate.\n"
    "HONESTY AND TONE RULES:\n"
    "- Never mislead: the spoken seeds must fully support every claim.\n"
    "- You may dramatize and sharpen the truth, but never fabricate a fact, "
    "number, name, or event absent from the spoken seeds.\n"
    "- Use only numbers that appear in the spoken seeds.\n"
    "- Never explicit, crude, or demeaning.\n"
    "- Never boring, generic, or cliched. BANNED: \"You won't believe…\", "
    "\"This will blow your mind…\", \"The hidden pattern behind…\", "
    "\"What most people miss…\", \"The real reason why…\".\n"
    "SENTENCE AND ORDER RULES:\n"
    "- Each slide is one complete sentence or a tight clause that can stand alone.\n"
    "- A single spoken sentence may occupy TWO consecutive slides only — never 3+.\n"
    "- Never leave a mid-clause scrap, [music] tag, or unfinished thought.\n"
    "- The whole deck must advance ONE argument from its chosen topic. Never turn "
    "nearby transcript mentions into a roundup of different tactics.\n"
    "- Place the selected hook for performance: on the cover when it opens a strong "
    "curiosity loop, in the middle when it is a reveal, or at the end when it is "
    "the payoff. Use it once, then make every other slide support its promise.\n"
    "- Build a causal swipe flow: hook/setup → problem/tension → explanation/proof "
    "→ payoff/action. Every slide must make the next slide feel necessary.\n"
    "SHAPE EXAMPLE (do not copy): spoken \"ghee business… market value of 42 "
    "billion… A2 growth rate is three times\" → slides like \"Built for a $42B ghee "
    "market.\" / \"Most brands still sell at ₹600–800 a litre.\" / "
    "\"A2 is growing 3x faster than the rest.\"\n"
)
_THEME_CHUNK_CHARS = 14_000
_THEME_CHUNK_OVERLAP_CUES = 3
# Gemini requests run in worker threads and the SDK does not time out by default,
# so a stalled connection would pin its thread — and the background carousel slot
# holding it — forever. Bound every request instead.
_LLM_REQUEST_TIMEOUT_MS = 25_000
# Railway's public edge (carousel → backend) 502s long extracts. Cap each
# provider hop so Gemini/Claude cannot hold the socket for minutes.
_LLM_ATTEMPT_TIMEOUT_SEC = 25.0
# OpenRouter regularly returns successful Claude responses in 25–50 seconds.
# Give that async, cancellable HTTP hop enough time without relaxing the bound
# on direct SDK calls, whose worker threads cannot be cancelled by asyncio.
_OPENROUTER_ATTEMPT_TIMEOUT_SEC = 60.0
# A correction is optional polish. Never discard a valid first draft or push
# the request past the browser/proxy budget because the polish call is slow.
_THEME_CORRECTION_TIMEOUT_SEC = 30.0
# Dense full-talk outline budget: enough global coverage without paying for
# filler/music cues that do not change theme boundaries.
_THEME_OUTLINE_MAX_CHARS = 18_000
# Opus/Sonnet via OpenRouter often wrap themes in {"items":[...]} with long
# insight-led titles/summaries. 1100 tokens truncated mid-JSON → empty parse.
_THEME_MAX_OUTPUT_TOKENS = 4_096
_THEME_FILLER_CUE_RE = re.compile(
    r"^(?:"
    r"\[?(?:music|applause|laughter|silence|inaudible|blank(?:\s+audio)?)\]?"
    r"|\([^)]*(?:music|applause|laughter)[^)]*\)"
    r")$",
    re.IGNORECASE,
)


def _loads_json_array(text: str) -> list[Any]:
    """Parse a JSON array from model text (tolerates markdown fences)."""
    m = re.search(r"\[[\s\S]*\]", text or "")
    if not m:
        return []
    try:
        raw = json.loads(m.group())
    except json.JSONDecodeError:
        return []
    return raw if isinstance(raw, list) else []


def _llm_has_any_key(
    *,
    api_key: str | None = None,
    claude_api_key: str | None = None,
    openrouter_api_key: str | None = None,
    openrouter_model: str = "",
) -> bool:
    or_ready = bool((openrouter_api_key or "").strip() and (openrouter_model or "").strip())
    return bool(
        or_ready
        or (claude_api_key or "").strip()
        or (api_key or "").strip()
    )


async def _llm_complete_json(
    *,
    prompt: str,
    system: str = (
        "You are an expert short-form video editor and Instagram carousel copywriter. "
        "Return ONLY valid JSON."
    ),
    temperature: float = 0.3,
    max_tokens: int = 4096,
    api_key: str | None = None,
    model: str = "",
    claude_api_key: str | None = None,
    claude_model: str = "",
    provider: str = "auto",
    openrouter_api_key: str | None = None,
    openrouter_model: str = "",
    openrouter_base_url: str = "",
    json_root: str = "object",
) -> tuple[str, str]:
    """Return ``(raw_text, provider)`` with OpenRouter / Claude / Gemini selection.

    ``provider``:
    - ``auto``: Claude (key) → OpenRouter → Gemini
    - ``openrouter``: OpenRouter first, then Claude → Gemini on failure
    - ``claude``: Anthropic first; OpenRouter twin if the direct call fails
    - ``gemini``: Gemini first; OpenRouter twin if the direct call fails
    """
    import asyncio

    from app.llm.carousel_llm import (
        DEFAULT_CLAUDE_MODEL,
        normalize_carousel_llm_provider,
        openrouter_slug_for_direct,
        prefer_openrouter_first,
    )

    pref = normalize_carousel_llm_provider(provider)
    or_key = (openrouter_api_key or "").strip()
    or_model = (openrouter_model or "").strip()
    or_base = (openrouter_base_url or "").strip() or "https://openrouter.ai/api/v1"
    claude_key = (claude_api_key or "").strip()
    gemini_key = (api_key or "").strip()
    if pref == "claude":
        or_model = openrouter_slug_for_direct("claude", claude_model) or or_model
    elif pref == "gemini":
        or_model = openrouter_slug_for_direct("gemini", model) or or_model

    errors: list[str] = []

    async def try_openrouter() -> tuple[str, str]:
        from app.llm.openrouter import complete_json

        text = await complete_json(
            prompt,
            model=or_model,
            api_key=or_key,
            base_url=or_base,
            system=system,
            temperature=temperature,
            max_tokens=max_tokens,
            timeout=_OPENROUTER_ATTEMPT_TIMEOUT_SEC,
            json_root=json_root,
        )
        return text.strip(), "openrouter"

    async def try_claude() -> tuple[str, str]:
        from anthropic import Anthropic

        def generate_claude() -> str:
            client = Anthropic(api_key=claude_key)
            response = client.messages.create(
                model=(claude_model or DEFAULT_CLAUDE_MODEL).strip(),
                max_tokens=max_tokens,
                system=system,
                messages=[{"role": "user", "content": prompt}],
            )
            return "".join(
                block.text
                for block in response.content
                if getattr(block, "type", "") == "text"
            )

        return (await asyncio.to_thread(generate_claude)).strip(), "claude"

    async def try_gemini() -> tuple[str, str]:
        from google import genai
        from google.genai import types

        client = genai.Client(
            api_key=gemini_key,
            http_options=types.HttpOptions(timeout=_LLM_REQUEST_TIMEOUT_MS),
        )
        resp = await asyncio.to_thread(
            client.models.generate_content,
            model=model,
            contents=[types.Content(role="user", parts=[types.Part(text=prompt)])],
            config=types.GenerateContentConfig(
                temperature=temperature,
                response_mime_type="application/json",
            ),
        )
        return (resp.text or "").strip(), "gemini"

    order: list[str] = []
    if pref == "auto":
        if claude_key:
            order.append("claude")
        if or_key and or_model:
            order.append("openrouter")
        if gemini_key:
            order.append("gemini")
    elif pref == "openrouter":
        # An explicit picker choice is a contract, not a suggestion. Cascading
        # through broken direct-provider credentials turned one clear failure
        # into a multi-minute request that outlived the browser.
        order.append("openrouter")
    elif pref == "claude":
        # Prefer Anthropic, then the OpenRouter twin. Arena picker ids such as
        # claude-fable-5 are not valid Messages API models; without this hop
        # the extract/copy path falls through to heuristic junk.
        if or_key and or_model and prefer_openrouter_first("claude", claude_model):
            order.append("openrouter")
            order.append("claude")
        else:
            order.append("claude")
            if or_key and or_model:
                order.append("openrouter")
    elif pref == "gemini":
        # Google generateContent on preview/pro models routinely exceeds the
        # Railway public-proxy budget and surfaces as 502 on /pipeline/extract.
        # OpenRouter's google/* twins finish in time; try them first.
        if or_key and or_model and prefer_openrouter_first("gemini", model):
            order.append("openrouter")
            order.append("gemini")
        else:
            order.append("gemini")
            if or_key and or_model:
                order.append("openrouter")

    runners = {
        "openrouter": try_openrouter,
        "claude": try_claude,
        "gemini": try_gemini,
    }
    ready = {
        "openrouter": bool(or_key and or_model),
        "claude": bool(claude_key),
        "gemini": bool(gemini_key and (model or "").strip()),
    }

    from app.search.carousel_trace import carousel_log

    carousel_log(
        "llm_call_start",
        preferred=pref,
        order=",".join(order) or "-",
        ready_openrouter=ready["openrouter"],
        ready_claude=ready["claude"],
        ready_gemini=ready["gemini"],
        openrouter_model=or_model or "-",
        claude_model=(claude_model or DEFAULT_CLAUDE_MODEL).strip() or "-",
        gemini_model=(model or "").strip() or "-",
        max_tokens=max_tokens,
        json_root=json_root,
        prompt_chars=len(prompt or ""),
    )

    for name in order:
        if not ready.get(name):
            if pref != "auto" and name == pref:
                errors.append(f"{name} not configured")
                carousel_log(
                    "llm_provider_skip",
                    level=logging.WARNING,
                    provider=name,
                    reason="not_configured",
                )
            else:
                carousel_log(
                    "llm_provider_skip",
                    provider=name,
                    reason="not_ready",
                )
            continue
        attempt_timeout = (
            _OPENROUTER_ATTEMPT_TIMEOUT_SEC
            if name == "openrouter"
            else _LLM_ATTEMPT_TIMEOUT_SEC
        )
        hop_started = time.perf_counter()
        carousel_log(
            "llm_provider_attempt",
            provider=name,
            timeout_sec=attempt_timeout,
            model=(
                or_model
                if name == "openrouter"
                else (claude_model or DEFAULT_CLAUDE_MODEL)
                if name == "claude"
                else model
            ),
        )
        try:
            text, provider_name = await asyncio.wait_for(
                runners[name](), timeout=attempt_timeout
            )
            elapsed_ms = (time.perf_counter() - hop_started) * 1000.0
            carousel_log(
                "llm_provider_ok",
                provider=provider_name,
                elapsed_ms=elapsed_ms,
                response_chars=len(text or ""),
            )
            return text, provider_name
        except Exception as exc:  # noqa: BLE001
            elapsed_ms = (time.perf_counter() - hop_started) * 1000.0
            err_type = type(exc).__name__
            # asyncio.TimeoutError stringifies to "" — make the reason explicit.
            if isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
                err_msg = f"timeout_after_{attempt_timeout:.0f}s"
            else:
                err_msg = (str(exc) or err_type)[:160]
            errors.append(f"{name}: {err_msg}")
            carousel_log(
                "llm_provider_fail",
                level=logging.WARNING,
                provider=name,
                elapsed_ms=elapsed_ms,
                timeout_sec=attempt_timeout,
                error_type=err_type,
                error=err_msg,
            )
            logger.warning(
                "Carousel LLM provider %s failed (%s) — trying next",
                name,
                err_msg,
            )

    detail = "; ".join(errors) if errors else "none configured"
    carousel_log(
        "llm_call_exhausted",
        level=logging.WARNING,
        preferred=pref,
        detail=detail[:300],
    )
    raise RuntimeError(f"No carousel LLM provider succeeded ({detail})")


def snap_themes_to_cues(
    themes: list[dict[str, Any]],
    cues: list[tuple[float, float | None, str]],
) -> list[dict[str, Any]]:
    """Snap starts to cue beginnings; remove overlaps; keep chronological order."""
    if not themes:
        return []
    cue_starts = sorted({float(s) for s, _, t in cues if (t or "").strip()})
    if not cue_starts:
        return themes[:_MAX_THEMES]

    cleaned: list[dict[str, Any]] = []
    prev_end = -1.0
    for raw in sorted(themes, key=lambda t: float(t.get("start_sec") or 0)):
        start = float(raw.get("start_sec") or 0)
        end = raw.get("end_sec")
        end_f = float(end) if end is not None else None
        # Snap start to nearest cue at or before requested start (context start).
        snapped = max((c for c in cue_starts if c <= start + 0.05), default=cue_starts[0])
        if snapped < prev_end - 0.05:
            snapped = prev_end
        if end_f is not None and end_f <= snapped:
            end_f = None
        if end_f is not None and prev_end >= 0 and end_f <= prev_end:
            continue
        item = dict(raw)
        item["start_sec"] = snapped
        item["end_sec"] = end_f
        item["theme_id"] = item.get("theme_id") or f"theme_{len(cleaned) + 1}"
        cleaned.append(item)
        prev_end = end_f if end_f is not None else snapped + 1.0
        if len(cleaned) >= _MAX_THEMES:
            break
    return cleaned


_THEME_SYSTEM = (
    "You are an expert business and entrepreneurship editor for short-form video. "
    "Identify commercially meaningful ideas, strategies, mistakes, and outcomes "
    "without inventing claims. Return ONLY valid JSON."
)
_THEME_FRAGMENT_OPENERS = (
    "and ",
    "but ",
    "even if ",
    "go ",
    "i think ",
    "it is ",
    "it was ",
    "it wasn't ",
    "now ",
    "so ",
    "then ",
    "there is ",
    "this is ",
    "we have ",
    "you know ",
)


def _theme_generation_prompt(
    *,
    transcript: str,
    video_name: str,
    scope_note: str = "",
    candidate_pass: bool = False,
) -> str:
    count_rule = (
        "Return 2–5 candidate themes from THIS CHUNK for a later full-talk synthesis."
        if candidate_pass
        else (
            "Return 3–8 chronological, non-overlapping themes. Use fewer themes when the "
            "conversation has fewer genuine shifts; do not invent themes to reach a quota."
        )
    )
    return (
        "You analyze a long-form interview or podcast transcript and segment it into "
        "clear, substantively distinct discussion themes for a business and "
        "entrepreneurship social carousel studio.\n"
        f"Video: {video_name or '(untitled)'}\n"
        f"{scope_note}"
        "A theme is a sustained discussion of one primary subject or policy area—not a "
        "single remark, keyword, anecdote, question, or generic label. For example, "
        "economic development, social protection, and foreign policy are separate themes "
        "when the speaker develops each as a meaningful discussion.\n"
        "Editorial lens: actively recognize business and entrepreneurship ideas such as "
        "customer acquisition, distribution, product, pricing, sales, growth, capital, "
        "profitability, operations, hiring, leadership, market strategy, wealth creation, "
        "policy impact on enterprise, founder mistakes, contrarian lessons, and actionable "
        "playbooks. These ideas may be expressed without standard business jargon.\n"
        "Hard rules:\n"
        "- Read all transcript text supplied in this prompt before deciding the themes.\n"
        "- Separate themes by meaning and subject, not by equal time intervals or arbitrary "
        "transcript chunks. Detect genuine topic shifts.\n"
        "- Each theme must have one clear primary focus that is meaningfully different from "
        "every other theme. Themes may be loosely related through the overall conversation, "
        "but must not overlap, duplicate, restate, or nest the same central idea.\n"
        "- Apply an exclusivity test: if two proposed themes could reasonably share the same "
        "title or summary, merge them; if they address distinct policy areas, problems, "
        "arguments, or outcomes, keep them separate.\n"
        "- Keep each theme internally coherent and assign each transcript passage to at most "
        "one theme. Do not mix unrelated subjects into a broad catch-all theme.\n"
        "- Start and end at natural conversational boundaries, never mid-sentence or "
        "mid-argument. Use cue timestamps from the transcript.\n"
        "- Titles must be concise, specific, complete English phrases that name the actual "
        "subject discussed; avoid vague titles such as 'Key Insights' or 'Looking Ahead'.\n"
        "- NEVER use a raw transcript fragment as a title. Remove conversational lead-ins "
        "such as 'now', 'so', 'I think', 'it was', 'go', or speaker-turn markers like '>>'.\n"
        "- Prefer an insight-led title when the transcript supports one: frame the speaker's "
        "actual thesis, strategy, mistake, opportunity, or outcome rather than merely naming "
        "a category. For example, 'Why Most Customer Acquisition Fails' is stronger than "
        "'Customer Acquisition', and 'Building a ₹100 Crore Business from Zero' is stronger "
        "than 'Business Growth'. Do not copy these examples unless the transcript says them.\n"
        "- Exercise editorial creativity in choosing the most compelling set of themes and "
        "wording their titles, while remaining faithful to the speaker's actual meaning. "
        "Never invent a claim, number, promise, or business lesson.\n"
        "- Summaries must be polished editorial prose stating the distinct argument, issue, "
        "or perspective—not copied transcript text, speaker markers, or keyword lists.\n"
        f"- {count_rule}\n"
        "Self-check before returning (fix every defect in-place; do not emit a weak draft):\n"
        "- Titles are Title Case or sentence case with a capital first letter; none begin with "
        "now/so/I think/it was/go/and/but.\n"
        "- No title or summary contains '>>' or raw caption glue.\n"
        "- Each summary is at least one clear editorial sentence (8+ words).\n"
        "- start_sec < end_sec, chronological, non-overlapping theme windows.\n"
        "- Near-duplicate titles are merged before return.\n"
        "Return ONLY a top-level JSON array. Each object must contain "
        "theme_id, title, start_sec, end_sec, summary.\n\n"
        f"Transcript:\n{transcript}"
    )


def _theme_quality_issues(
    themes: list[dict[str, Any]],
    *,
    expected_min: int = 3,
) -> list[str]:
    """Return concrete editorial defects that justify one corrective LLM pass."""
    issues: list[str] = []
    if len(themes) < expected_min:
        issues.append(f"Only {len(themes)} themes were returned; expected at least {expected_min}.")
    if len(themes) > _MAX_THEMES:
        issues.append(f"{len(themes)} themes exceed the {_MAX_THEMES}-theme limit.")

    title_tokens: list[set[str]] = []
    for i, theme in enumerate(themes):
        title = " ".join(str(theme.get("title") or "").split()).strip()
        summary = " ".join(str(theme.get("summary") or "").split()).strip()
        lower = title.lower()
        words = re.findall(r"[A-Za-z0-9₹$%]+", title)
        if ">>" in title or not 2 <= len(words) <= 12:
            issues.append(f"Theme {i + 1} title is fragmentary or poorly sized: {title!r}.")
        if title[:1].islower() or lower.startswith(_THEME_FRAGMENT_OPENERS):
            issues.append(f"Theme {i + 1} title begins like raw speech: {title!r}.")
        if ">>" in summary or len(summary.split()) < 8:
            issues.append(f"Theme {i + 1} summary is copied, fragmentary, or too thin.")
        start = _as_float(theme.get("start_sec"))
        end = _as_float(theme.get("end_sec"))
        if start is None or (end is not None and end <= start):
            issues.append(f"Theme {i + 1} has an invalid timestamp range.")
        title_tokens.append(
            {
                token
                for token in re.findall(r"[a-z0-9]+", lower)
                if token not in _TOPIC_OVERLAP_STOP and len(token) > 2
            }
        )

    for i, left in enumerate(title_tokens):
        for j in range(i + 1, len(title_tokens)):
            right = title_tokens[j]
            if left and right and len(left & right) / len(left | right) >= 0.65:
                issues.append(f"Themes {i + 1} and {j + 1} have near-duplicate titles.")
    return issues


def _theme_needs_llm_correction(issues: list[str]) -> bool:
    """Only pay for a second LLM hop when the draft is clearly unusable."""
    severe = (
        "raw speech",
        "fragmentary",
        "copied, fragmentary",
        "poorly sized",
        "invalid timestamp",
    )
    return any(any(marker in issue for marker in severe) for issue in issues)

def _theme_synthesis_prompt(
    *,
    video_name: str,
    candidates: list[dict[str, Any]],
    outline: str,
) -> str:
    return (
        "Create the FINAL theme map for the full interview from chunk-level candidates and "
        "an evenly sampled timed outline of the entire talk.\n"
        f"Video: {video_name or '(untitled)'}\n"
        "Read across the whole timeline. Merge duplicates across chunks, discard transcript "
        "fragments, and separate genuinely different business ideas. Do not preserve chunk "
        "boundaries or divide the talk into equal-duration buckets.\n"
        "Return 3–8 chronological, non-overlapping themes with natural topic-shift boundaries. "
        "Titles must be polished, insight-led business or entrepreneurship ideas—not quotes or "
        "raw speech. Summaries must explain the speaker's distinct argument in editorial prose. "
        "Use only claims and numbers supported by the supplied material.\n"
        "Return ONLY a top-level JSON array. Each object must contain "
        "theme_id, title, start_sec, end_sec, summary.\n\n"
        f"Chunk candidates:\n{json.dumps(candidates[:40], ensure_ascii=False)}\n\n"
        f"Full-talk timed outline:\n{outline}"
    )


def _theme_correction_prompt(
    *,
    video_name: str,
    themes: list[dict[str, Any]],
    issues: list[str],
    outline: str,
) -> str:
    return (
        "Rewrite this weak theme map into a production-quality business interview theme map.\n"
        f"Video: {video_name or '(untitled)'}\n"
        "Quality defects detected:\n"
        + "\n".join(f"- {issue}" for issue in issues[:20])
        + "\n\nCorrect every defect. Use the full-talk outline to replace transcript-fragment "
        "titles with concise, insight-led themes; merge semantic duplicates; preserve distinct "
        "business ideas; and place boundaries at real topic shifts rather than equal intervals. "
        "Never invent claims or numbers. Return 3–8 chronological, non-overlapping themes.\n"
        "Return ONLY a top-level JSON array. Each object must contain "
        "theme_id, title, start_sec, end_sec, summary.\n\n"
        f"Weak draft:\n{json.dumps(themes, ensure_ascii=False)}\n\n"
        f"Full-talk timed outline:\n{outline}"
    )


async def build_harmonized_themes(
    *,
    cues: list[tuple[float, float | None, str]],
    video_name: str,
    search_entity: str | None = None,
    api_key: str | None,
    model: str,
    claude_api_key: str | None = None,
    claude_model: str = "",
    provider: str = "auto",
    openrouter_api_key: str | None = None,
    openrouter_model: str = "",
    openrouter_base_url: str = "",
) -> tuple[list[dict[str, Any]], str, str | None]:
    """Return normal narrative themes (search_entity is ignored — no reframing)."""
    del search_entity  # presence checks live in the router; themes stay video-native
    usable_cues = [(s, e, t) for s, e, t in cues if (t or "").strip()]
    if not usable_cues:
        return [], "empty", "This video doesn’t have a transcript yet. Wait until indexing finishes, then try again."

    from app.search.carousel_trace import carousel_log, carousel_step

    warning: str | None = None
    if _llm_has_any_key(
        api_key=api_key,
        claude_api_key=claude_api_key,
        openrouter_api_key=openrouter_api_key,
        openrouter_model=openrouter_model,
    ):
        try:
            outline = _condense_transcript_outline(
                usable_cues, max_chars=_THEME_OUTLINE_MAX_CHARS
            )
            carousel_log(
                "themes_llm_start",
                provider=provider,
                cue_count=len(usable_cues),
                prompt_chars=len(outline),
                strategy="single_full_talk",
                openrouter_model=(openrouter_model or "").strip() or "-",
                claude_model=(claude_model or "").strip() or "-",
                gemini_model=(model or "").strip() or "-",
            )
            with carousel_step(
                "themes_full_talk",
                cue_count=len(usable_cues),
                prompt_chars=len(outline),
            ):
                text, llm_source = await _llm_complete_json(
                    prompt=_theme_generation_prompt(
                        transcript=outline,
                        video_name=video_name,
                        scope_note=(
                            "This is a substance-filtered, timestamped outline spanning the entire "
                            "talk (filler/music cues removed). Build the final theme map directly "
                            "from it.\n"
                        ),
                    ),
                    system=_THEME_SYSTEM,
                    temperature=0.3,
                    max_tokens=_THEME_MAX_OUTPUT_TOKENS,
                    api_key=api_key,
                    model=model,
                    claude_api_key=claude_api_key,
                    claude_model=claude_model or "claude-sonnet-4-20250514",
                    provider=provider,
                    openrouter_api_key=openrouter_api_key,
                    openrouter_model=openrouter_model,
                    openrouter_base_url=openrouter_base_url,
                    json_root="array",
                )
                themes = _parse_themes_json(text)
                if not themes:
                    _raise_theme_parse_error(
                        text,
                        source=llm_source,
                        stage="full-talk generation",
                    )
                carousel_log(
                    "themes_full_talk_parsed",
                    themes=len(themes),
                    provider=llm_source,
                )

            if themes:
                themes = snap_themes_to_cues(themes, cues)
            issues = _theme_quality_issues(themes)
            if issues and _theme_needs_llm_correction(issues):
                carousel_log(
                    "themes_quality_issues",
                    level=logging.WARNING,
                    issue_count=len(issues),
                    issues="; ".join(str(i)[:80] for i in issues[:4]),
                )
                try:
                    with carousel_step("themes_correction", issue_count=len(issues)):
                        text, corrected_source = await asyncio.wait_for(
                            _llm_complete_json(
                                prompt=_theme_correction_prompt(
                                    video_name=video_name,
                                    themes=themes,
                                    issues=issues,
                                    outline=outline,
                                ),
                                system=_THEME_SYSTEM,
                                temperature=0.2,
                                max_tokens=_THEME_MAX_OUTPUT_TOKENS,
                                api_key=api_key,
                                model=model,
                                claude_api_key=claude_api_key,
                                claude_model=claude_model or "claude-sonnet-4-20250514",
                                provider=provider,
                                openrouter_api_key=openrouter_api_key,
                                openrouter_model=openrouter_model,
                                openrouter_base_url=openrouter_base_url,
                                json_root="array",
                            ),
                            timeout=_THEME_CORRECTION_TIMEOUT_SEC,
                        )
                        corrected = snap_themes_to_cues(_parse_themes_json(text), cues)
                        corrected_issues = _theme_quality_issues(corrected)
                        if corrected and len(corrected_issues) < len(issues):
                            themes = corrected
                            issues = corrected_issues
                            llm_source = corrected_source
                except Exception as exc:  # noqa: BLE001
                    carousel_log(
                        "themes_correction_skipped",
                        level=logging.WARNING,
                        error_type=type(exc).__name__,
                        error=str(exc)[:160] or type(exc).__name__,
                    )
                if issues:
                    warning = "Theme quality remained below target after the bounded corrective pass."
            elif issues:
                carousel_log(
                    "themes_quality_soft_issues_kept",
                    issue_count=len(issues),
                    issues="; ".join(str(i)[:80] for i in issues[:4]),
                )
            if themes:
                for t in themes:
                    t["harmonized"] = False
                    t["search_entity"] = None
                carousel_log(
                    "themes_llm_ok",
                    provider=llm_source,
                    theme_count=len(themes),
                    warning=warning or "-",
                )
                return themes, llm_source, warning
        except Exception as exc:  # noqa: BLE001
            logger.warning("carousel theme LLM failed: %s", exc)
            carousel_log(
                "themes_llm_fail",
                level=logging.WARNING,
                error_type=type(exc).__name__,
                error=str(exc)[:200] or type(exc).__name__,
            )
            warning = str(exc)[:160]
    else:
        warning = "Claude/Gemini/OpenRouter unavailable — using transcript buckets"
        carousel_log("themes_llm_unavailable", level=logging.WARNING)

    fallback = fallback_topics_from_cues(cues, max_topics=6)
    themes = []
    for i, row in enumerate(fallback):
        title = str(row.get("title") or f"Segment {i + 1}")
        themes.append(
            {
                "theme_id": f"theme_{i + 1}",
                "title": title[:120],
                "start_sec": float(row.get("start_sec") or 0),
                "end_sec": row.get("end_sec"),
                "summary": str(row.get("explanation") or "")[:500],
                "harmonized": False,
                "search_entity": None,
            }
        )
    themes = snap_themes_to_cues(themes, cues)
    carousel_log(
        "themes_fallback",
        level=logging.WARNING,
        theme_count=len(themes),
        warning=warning or "-",
    )
    return themes, "fallback", warning


async def _llm_themes(
    *,
    transcript: str,
    video_name: str,
    api_key: str,
    model: str,
) -> list[dict[str, Any]]:
    import asyncio

    from google import genai
    from google.genai import types

    prompt = (
        "You segment a video transcript into distinct narrative themes for a carousel studio.\n"
        f"Video: {video_name or '(untitled)'}\n"
        "Hard rules:\n"
        "- Start each theme at the beginning of a logical context (never mid-sentence).\n"
        "- Theme titles MUST be complete phrases (never end with to/be/in/of/and/the…).\n"
        "- Theme titles and summaries MUST be in natural English "
        "(translate if the transcript is Hindi/Hinglish/other).\n"
        "- Zero overlap between themes; chronological; no duplicate phrasing.\n"
        "- Group by natural narrative shifts.\n"
        "Return ONLY JSON array (3–8 objects). Each object:\n"
        '- theme_id (string like "theme_1")\n'
        "- title (short, English)\n"
        "- start_sec (number)\n"
        "- end_sec (number or null)\n"
        "- summary (1–2 sentences, English)\n\n"
        f"Transcript:\n{transcript}"
    )

    client = genai.Client(
        api_key=api_key,
        http_options=types.HttpOptions(timeout=_LLM_REQUEST_TIMEOUT_MS),
    )
    resp = await asyncio.to_thread(
        client.models.generate_content,
        model=model,
        contents=[types.Content(role="user", parts=[types.Part(text=prompt)])],
        config=types.GenerateContentConfig(
            temperature=0.3,
            response_mime_type="application/json",
        ),
    )
    return _parse_themes_json((resp.text or "").strip())


async def _claude_themes(
    *,
    transcript: str,
    video_name: str,
    api_key: str,
    model: str,
) -> list[dict[str, Any]]:
    """Use Claude for higher-quality narrative titles and summaries."""
    import asyncio

    from anthropic import Anthropic

    prompt = (
        "You segment a video transcript into distinct narrative themes for a social carousel studio.\n"
        f"Video: {video_name or '(untitled)'}\n"
        "Hard rules:\n"
        "- Start each theme at a logical context boundary, never mid-sentence.\n"
        "- Titles must be concise, specific, complete English phrases.\n"
        "- Summaries should explain the viewer-relevant idea, not merely repeat words.\n"
        "- Use 3–8 chronological, non-overlapping themes with no duplicate angles.\n"
        "Return ONLY a JSON array. Each object must contain theme_id, title, start_sec, end_sec, summary.\n\n"
        f"Transcript:\n{transcript}"
    )

    def generate() -> str:
        client = Anthropic(api_key=api_key)
        response = client.messages.create(
            model=model,
            max_tokens=1800,
            system="You are an expert short-form video editor and narrative copywriter.",
            messages=[{"role": "user", "content": prompt}],
        )
        return "".join(
            block.text for block in response.content if getattr(block, "type", "") == "text"
        )

    return _parse_themes_json(await asyncio.to_thread(generate))


def _strip_markdown_json_fence(text: str) -> str:
    """Remove ``` / ```json wrappers OpenRouter Claude often adds around JSON."""
    cleaned = (text or "").strip()
    if not cleaned.startswith("```"):
        return cleaned
    cleaned = re.sub(r"^```(?:json|JSON)?\s*", "", cleaned)
    cleaned = re.sub(r"\s*```\s*$", "", cleaned)
    return cleaned.strip()


def _parse_themes_json(text: str) -> list[dict[str, Any]]:
    raw: Any = None
    cleaned = _strip_markdown_json_fence(text)
    try:
        raw = json.loads(cleaned)
    except json.JSONDecodeError:
        m = re.search(r"\[[\s\S]*\]", cleaned)
        if m:
            try:
                raw = json.loads(m.group())
            except json.JSONDecodeError:
                raw = None
        if raw is None:
            # Truncated {"items":[...]} — salvage complete objects if any closed.
            obj_m = re.search(r"\{[\s\S]*", cleaned)
            if obj_m:
                blob = obj_m.group()
                for key in ("themes", "items", "results"):
                    key_m = re.search(rf'"{key}"\s*:\s*\[', blob)
                    if not key_m:
                        continue
                    arr_start = key_m.end() - 1
                    # Walk objects until the last complete `{...}` before truncation.
                    objs: list[Any] = []
                    depth = 0
                    start = -1
                    in_str = False
                    esc = False
                    for i, ch in enumerate(blob[arr_start + 1 :], start=arr_start + 1):
                        if in_str:
                            if esc:
                                esc = False
                            elif ch == "\\":
                                esc = True
                            elif ch == '"':
                                in_str = False
                            continue
                        if ch == '"':
                            in_str = True
                            continue
                        if ch == "{":
                            if depth == 0:
                                start = i
                            depth += 1
                        elif ch == "}":
                            depth -= 1
                            if depth == 0 and start >= 0:
                                try:
                                    objs.append(json.loads(blob[start : i + 1]))
                                except json.JSONDecodeError:
                                    pass
                                start = -1
                        elif ch == "]" and depth == 0:
                            break
                    if objs:
                        raw = objs
                        break
    if isinstance(raw, dict):
        for key in ("themes", "items", "results"):
            candidate = raw.get(key)
            if isinstance(candidate, list):
                raw = candidate
                break
    if not isinstance(raw, list):
        return []
    out: list[dict[str, Any]] = []
    seen_titles: set[str] = set()
    for i, row in enumerate(raw):
        if not isinstance(row, dict):
            continue
        title = str(row.get("title") or "").strip()
        if not title:
            continue
        title = _complete_theme_title(title)
        key = title.lower()
        if key in seen_titles:
            continue
        seen_titles.add(key)
        start = _as_float(row.get("start_sec", row.get("start")))
        end = _as_float(row.get("end_sec", row.get("end")))
        summary = str(row.get("summary") or row.get("explanation") or "").strip()
        out.append(
            {
                "theme_id": str(row.get("theme_id") or f"theme_{i + 1}")[:64],
                "title": title[:120],
                "start_sec": start if start is not None else 0.0,
                "end_sec": end if end is not None and (start is None or end > start) else None,
                "summary": (summary or f"Covers {title}.")[:500],
            }
        )
        if len(out) >= _MAX_THEMES:
            break
    return out


def _raise_theme_parse_error(text: str, *, source: str, stage: str) -> None:
    """Log a safe response excerpt and prevent silent transcript-bucket fallback."""
    excerpt = " ".join((text or "").split())[:500]
    logger.warning(
        "Carousel theme parse failed provider=%s stage=%s response=%r",
        source,
        stage,
        excerpt,
    )
    raise RuntimeError(
        f"{source or 'LLM'} returned no valid themes during {stage}; "
        "the response was not valid theme JSON"
    )


def extract_hooks_and_topics(
    cues: list[tuple[float, float | None, str]],
    *,
    start_sec: float,
    end_sec: float | None,
    theme_title: str = "",
    theme_summary: str = "",
    english_cues: list[tuple[float, float | None, str]] | None = None,
) -> dict[str, Any]:
    """
    Hooks: candidate spoken windows (later analysed into punchy display hooks).
    Topics: thematic labels derived from the selected theme — not raw transcript dumps.

    When english_cues are provided (parallel English caption track), hooks are pulled
    from that track for the same time window. Otherwise prefers English lines already
    present in `cues` when the window is mixed-language.
    """
    primary = english_cues if english_cues else cues
    window = prefer_english_cues(_cues_in_range(primary, start_sec, end_sec))
    # Fall back to indexed cues if English alternate is empty in this window.
    if not window and english_cues:
        window = prefer_english_cues(_cues_in_range(cues, start_sec, end_sec))
    stitched = _stitch_complete_utterances(window)
    hooks = _pick_contextual_hooks(stitched)
    if english_cues:
        for h in hooks:
            h["english_source"] = "caption_track"
            h["translated"] = False
    topics = _topics_from_theme(
        theme_title=theme_title,
        theme_summary=theme_summary,
        hooks=hooks,
        stitched=stitched,
        theme_start=float(start_sec or 0),
        theme_end=end_sec,
    )
    return {
        "hooks": hooks[:_MAX_HOOKS],
        "topics": topics[:_MAX_TOPICS],
        "cue_count": len(window),
        "english_source": "caption_track" if english_cues else "indexed",
    }


async def extract_hooks_and_topics_async(
    cues: list[tuple[float, float | None, str]],
    *,
    start_sec: float,
    end_sec: float | None,
    theme_title: str = "",
    theme_summary: str = "",
    search_entity: str | None = None,
    api_key: str | None = None,
    model: str = "",
    claude_api_key: str | None = None,
    claude_model: str = "",
    provider: str = "auto",
    openrouter_api_key: str | None = None,
    openrouter_model: str = "",
    openrouter_base_url: str = "",
    english_cues: list[tuple[float, float | None, str]] | None = None,
    include_hooks: bool = True,
) -> dict[str, Any]:
    """Topics → subtopics → (optional) hooks, with English preference.

        Honors studio LLM selectability (Claude / OpenRouter / Gemini / auto).
        When ``include_hooks`` is False, stop after the topic tree so the studio
        can let the user pick topics before spending another LLM pass on hooks.
    """
    has_llm = _llm_has_any_key(
        api_key=api_key,
        claude_api_key=claude_api_key,
        openrouter_api_key=openrouter_api_key,
        openrouter_model=openrouter_model,
    )
    llm_prefers_claude = bool((claude_api_key or "").strip())
    # Prefer a parallel English track when the indexed window is non-English.
    window_indexed = _cues_in_range(cues, start_sec, end_sec)
    use_english_track = bool(english_cues) and (
        cues_need_english(window_indexed) or not window_indexed
    )
    active_english = english_cues if use_english_track else None

    base = extract_hooks_and_topics(
        cues,
        start_sec=start_sec,
        end_sec=end_sec,
        theme_title=theme_title,
        theme_summary=theme_summary,
        english_cues=active_english,
    )

    # Prefer indexed cues when the English alternate track is sparse/empty in-range
    # (that bug made Gemini "not read" the talk — 1–2 cues instead of dozens).
    primary_pool = prefer_english_cues(cues)
    if active_english:
        eng_window = _cues_in_range(active_english, start_sec, end_sec)
        idx_chars = len(compact_transcript(window_indexed, max_chars=200_000))
        eng_chars = len(compact_transcript(eng_window, max_chars=200_000))
        if eng_chars >= max(800, int(idx_chars * 0.45)) and len(eng_window) >= max(
            8, len(window_indexed) // 4
        ):
            primary_pool = active_english
        else:
            logger.warning(
                "english cue window too thin (eng=%d/%d chars vs idx=%d/%d) — using indexed transcript",
                len(eng_window),
                eng_chars,
                len(window_indexed),
                idx_chars,
            )
            primary_pool = cues

    window = _cues_in_range(primary_pool, start_sec, end_sec) or window_indexed
    window, used_start, used_end = _expand_thin_topic_window(
        primary_pool if primary_pool else cues,
        window,
        start_sec=float(start_sec or 0),
        end_sec=end_sec,
    )
    # Full timed cues for this theme window (chunked inside the topic LLM helper).
    full_transcript = compact_transcript(window, max_chars=200_000)
    stitched = _stitch_complete_utterances(window)
    cue_chars = len(full_transcript)
    cue_count = len(window)
    start_sec = used_start
    end_sec = used_end
    logger.info(
        "carousel topic extract: cues=%d chars=%d theme=%r window=%.1f–%s",
        cue_count,
        cue_chars,
        (theme_title or "")[:80],
        float(start_sec or 0),
        f"{float(end_sec):.1f}" if end_sec is not None else "end",
    )

    topic_tree: list[dict[str, Any]] = []
    topic_source = "none"
    chunks_used = 0
    llm_provider_used = "none"
    if has_llm and window:
        try:
            topic_tree, chunks_used, llm_provider_used = await _llm_topic_tree_from_cues(
                cues=window,
                theme_title=theme_title,
                theme_summary=theme_summary,
                search_entity=search_entity,
                api_key=api_key,
                model=model,
                claude_api_key=claude_api_key,
                claude_model=claude_model,
                provider=provider,
                openrouter_api_key=openrouter_api_key,
                openrouter_model=openrouter_model,
                openrouter_base_url=openrouter_base_url,
                theme_start=float(start_sec or 0),
                theme_end=end_sec,
            )
            if llm_provider_used == "claude":
                topic_source = "claude_chunked" if chunks_used > 1 else "claude"
            elif llm_provider_used == "openrouter":
                topic_source = "openrouter_chunked" if chunks_used > 1 else "openrouter"
            elif llm_provider_used == "gemini":
                topic_source = "llm_chunked" if chunks_used > 1 else "llm"
            else:
                topic_source = "none"
        except Exception as exc:  # noqa: BLE001
            logger.warning("topic tree generation failed: %s", exc)
            topic_tree = []
            topic_source = "error"

    if not topic_tree:
        # Fall back: heuristic buckets from ALL cues in window (not a truncated sample).
        fb = fallback_topics_from_cues(window, max_topics=min(10, max(4, cue_count // 8 or 4)))
        if fb:
            topic_tree = _flat_topics_to_tree(
                [
                    {
                        "id": f"topic_{i + 1}",
                        "text": t.get("title"),
                        "start_sec": t.get("start_sec"),
                        "end_sec": t.get("end_sec"),
                        "explanation": t.get("explanation"),
                        "subtopics": t.get("subtopics") or [],
                    }
                    for i, t in enumerate(fb)
                ]
            )
            # Promote nested fallback subtopics into tree shape.
            for node, raw in zip(topic_tree, fb):
                subs = []
                for j, sub in enumerate(raw.get("subtopics") or []):
                    if not isinstance(sub, dict):
                        continue
                    title = str(sub.get("title") or "").strip()
                    if not title:
                        continue
                    subs.append(
                        {
                            "id": f"{node['id']}_sub_{j + 1}",
                            "text": title[:120],
                            "start_sec": float(sub.get("start_sec") or node["start_sec"]),
                            "end_sec": sub.get("end_sec"),
                            "explanation": str(sub.get("explanation") or "")[:300],
                            "hooks": [],
                        }
                    )
                node["subtopics"] = subs
            topic_source = "fallback_cues"
        elif has_llm and full_transcript.strip():
            try:
                flat = await _llm_topics_from_theme(
                    theme_title=theme_title,
                    theme_summary=theme_summary,
                    transcript=full_transcript[:_TOPIC_CHUNK_CHARS],
                    search_entity=search_entity,
                    api_key=api_key,
                    model=model,
                    claude_api_key=claude_api_key,
                    claude_model=claude_model,
                    provider=provider,
                    openrouter_api_key=openrouter_api_key,
                    openrouter_model=openrouter_model,
                    openrouter_base_url=openrouter_base_url,
                    theme_start=float(start_sec or 0),
                    theme_end=end_sec,
                    hooks=base.get("hooks") or [],
                    stitched=stitched,
                ) or []
                topic_tree = _flat_topics_to_tree(flat)
                # Flat path still prefers Claude then Gemini inside _llm_complete_json.
                topic_source = "claude_flat" if llm_prefers_claude else "llm_flat"
                llm_provider_used = "claude" if llm_prefers_claude else "gemini"
            except Exception as exc:  # noqa: BLE001
                logger.warning("theme topic generation failed: %s", exc)

    # Heuristic first, then optional semantic merge — never collapse a rich talk.
    if len(topic_tree) >= 2:
        before = len(topic_tree)
        payload = [
            {
                "id": t.get("id"),
                "text": t.get("text"),
                "start_sec": t.get("start_sec"),
                "end_sec": t.get("end_sec"),
                "time_ranges": t.get("time_ranges"),
                "explanation": t.get("explanation"),
                "subtopics": t.get("subtopics"),
                "hooks": t.get("hooks"),
            }
            for t in topic_tree
        ]
        labels = heuristic_topic_dedupe(payload, threshold=0.62)
        source = "heuristic"
        if has_llm and len(labels) >= 2:
            try:
                semantic = await dedupe_topics_semantic(
                    labels,
                    theme_title=theme_title,
                    api_key=api_key,
                    model=model,
                    claude_api_key=claude_api_key,
                    claude_model=claude_model,
                    provider=provider,
                    openrouter_api_key=openrouter_api_key,
                    openrouter_model=openrouter_model,
                    openrouter_base_url=openrouter_base_url,
                )
                if semantic:
                    labels = semantic
                    source = "semantic"
            except Exception as exc:  # noqa: BLE001
                logger.warning("carousel topic semantic dedupe failed: %s", exc)
        by_text = {str(t.get("text") or "").strip().lower(): t for t in topic_tree}
        topic_tree = []
        for lab in labels:
            key = str(lab.get("text") or "").strip().lower()
            src = by_text.get(key) or lab
            if src is not lab:
                merged = dict(src)
                for field in ("time_ranges", "subtopics", "hooks", "explanation", "end_sec"):
                    if lab.get(field) and not merged.get(field):
                        merged[field] = lab.get(field)
                topic_tree.append(merged)
            else:
                topic_tree.append(src)
        logger.info(
            "carousel topic dedupe (%s): %d → %d",
            source,
            before,
            len(topic_tree),
        )

    # Every path above (LLM chunks, flat fallback, heuristic cues) must land on
    # one clean chronological, non-overlapping timeline before hooks are crafted.
    topic_tree = _normalize_topic_chronology(
        topic_tree,
        span_start=float(start_sec or 0),
        span_end=end_sec,
    )

    if not include_hooks:
        for topic in topic_tree:
            topic["hooks"] = []
            for sub in list(topic.get("subtopics") or []):
                sub["hooks"] = []
        base["hooks"] = []
        # Clear heuristic base hooks so English polish does not revive them.
        base["topics"] = _flatten_topic_tree(topic_tree)[:_MAX_TOPICS]
        base["topic_tree"] = _reindex_topic_tree(topic_tree)[:_MAX_TOPICS]
        multi_range = sum(
            1 for t in base["topic_tree"] if len(t.get("time_ranges") or []) >= 2
        )
        base["transcript_meta"] = {
            "cue_count": cue_count,
            "transcript_chars": cue_chars,
            "chunks_used": chunks_used,
            "topic_source": topic_source,
            "llm_provider": (
                llm_provider_used
                if llm_provider_used != "none"
                else (
                    "claude"
                    if llm_prefers_claude and has_llm
                    else ("gemini" if has_llm else "none")
                )
            ),
            "claude_preferred": llm_prefers_claude,
            "topic_tree_count": len(base["topic_tree"]),
            "flat_topic_count": len(base["topics"]),
            "hook_count": 0,
            "topics_with_multi_ranges": multi_range,
            "include_hooks": False,
            "verbatim_guard": {
                "checked": 0,
                "rejected_verbatim": 0,
                "rewritten": 0,
                "dropped": 0,
                "verbatim_kept": 0,
            },
            "empty_hook_sections": 0,
        }
        logger.info("carousel topic extract done (topics-only): %s", base["transcript_meta"])
        base = await ensure_english_display_texts(
            base,
            english_cues=english_cues,
            api_key=api_key,
            model=model,
            claude_api_key=claude_api_key,
            claude_model=claude_model,
            provider=provider,
            openrouter_api_key=openrouter_api_key,
            openrouter_model=openrouter_model,
            openrouter_base_url=openrouter_base_url,
        )
        return base

    # Select exact transcript windows first, then turn them into grounded display hooks.
    cue_corpus = [str(t or "") for _s, _e, t in window if (t or "").strip()]
    all_hooks: list[dict[str, Any]] = []
    used_angles: list[str] = []
    verbatim_stats_total = {
        "checked": 0,
        "rejected_verbatim": 0,
        "rewritten": 0,
        "dropped": 0,
        "verbatim_kept": 0,
    }
    hook_pool = primary_pool if primary_pool else cues
    for topic in topic_tree:
        topic_window = _cues_for_topic_ranges(
            hook_pool,
            topic,
            fallback_start=float(start_sec or 0),
            fallback_end=end_sec,
        ) or window
        topic_stitched = _stitch_complete_utterances(topic_window)
        topic_title = str(topic.get("text") or "")
        candidates = _pick_contextual_hooks(topic_stitched)
        if candidates and topic_title:
            # Prefer topic-relevant lines that also contain a strong business insight.
            candidates = sorted(
                candidates,
                key=lambda h: -(
                    _topic_text_overlap(topic_title, str(h.get("text") or ""))
                    + 0.35 * _business_hook_score(str(h.get("text") or ""))
                ),
            )
        candidates = candidates[:4]
        if not candidates:
            # Emergency: longest cues in the topic window so we never emit empty topics.
            candidates = _emergency_hook_candidates(topic_stitched or topic_window, limit=3)
        if not candidates:
            topic["hooks"] = []
            continue
        for h in candidates:
            h["topic_id"] = topic.get("id")
            h["topic_text"] = topic.get("text")
        topic_hooks: list[dict[str, Any]] = []
        if _llm_has_any_key(
            api_key=api_key,
            claude_api_key=claude_api_key,
            openrouter_api_key=openrouter_api_key,
            openrouter_model=openrouter_model,
        ):
            try:
                topic_hooks = await _llm_hooks_for_singular_topic(
                    hooks=candidates,
                    topic_title=topic_title,
                    topic_explanation=str(topic.get("explanation") or ""),
                    theme_title=theme_title,
                    theme_summary=theme_summary,
                    used_angles=used_angles,
                    api_key=api_key,
                    model=model,
                    claude_api_key=claude_api_key,
                    claude_model=claude_model,
                    provider=provider,
                    openrouter_api_key=openrouter_api_key,
                    openrouter_model=openrouter_model,
                    openrouter_base_url=openrouter_base_url,
                    max_hooks=2,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("topic hook crafting failed for %r: %s", topic_title, exc)
        if not topic_hooks:
            topic_hooks = heuristic_craft_hooks(
                candidates,
                theme_title=f"{theme_title}: {topic_title}".strip(": "),
            )[:2]
        verbatim_stats_total["checked"] += len(candidates)
        verbatim_stats_total["rewritten"] += len(topic_hooks)
        # Keep a useful local set; the global cap and final tree dedupe still
        # bound the shipped payload.
        topic_hooks = topic_hooks[:5]
        for h in topic_hooks:
            used_angles.append(str(h.get("text") or ""))
            all_hooks.append(h)
        topic["hooks"] = topic_hooks
        # Attach hooks to best-matching subtopic by time overlap when present.
        subs = list(topic.get("subtopics") or [])
        if subs:
            for h in topic_hooks:
                best = _best_subtopic_for_hook(h, subs)
                if best is not None:
                    h["subtopic_id"] = best.get("id")
                    h["subtopic_text"] = best.get("text")
                    best.setdefault("hooks", []).append(h)
                    # A mapped hook belongs to the most relevant subtopic,
                    # never to both the parent and child.
                    topic["hooks"] = [
                        existing
                        for existing in topic_hooks
                        if existing is not h
                    ]
            for sub in subs:
                sub.setdefault("hooks", [])

    # Structural guard: every returned topic/subtopic section must have ≥1 hook.
    topic_tree, all_hooks, prune_stats = _ensure_hooks_on_every_section(
        topic_tree,
        all_hooks=all_hooks,
        cues=hook_pool,
        theme_title=theme_title,
        cue_corpus=cue_corpus,
    )
    verbatim_stats_total["sections_pruned"] = prune_stats.get("pruned", 0)
    verbatim_stats_total["hooks_backfilled"] = prune_stats.get("backfilled", 0)

    if not all_hooks:
        # Legacy path: retain provenance but never expose raw cue dumps as hooks.
        if english_cues and any(needs_english(h.get("text", "")) for h in base["hooks"]):
            base["hooks"] = _swap_hooks_with_english_cues(base["hooks"], english_cues)
        base["hooks"] = heuristic_craft_hooks(
            list(base.get("hooks") or []),
            theme_title=theme_title,
        )
        all_hooks = list(base.get("hooks") or [])

    all_hooks = _dedupe_hook_list(all_hooks)

    all_hooks, guard_stats = enforce_non_verbatim_hooks(
        all_hooks,
        cue_corpus,
        theme_title=theme_title,
    )
    for key in ("rejected_verbatim", "rewritten", "dropped"):
        verbatim_stats_total[key] += guard_stats.get(key, 0)
    topic_tree = _sync_topic_tree_hooks(topic_tree, all_hooks)

    # The tree is authoritative: a hook must have exactly one placement.
    topic_tree = _dedupe_topic_tree_hooks(topic_tree)
    all_hooks = _hooks_from_topic_tree(topic_tree)
    # Re-id chronologically and flatten topics for legacy consumers.
    all_hooks.sort(key=lambda r: float(r.get("start_sec") or 0))
    for i, h in enumerate(all_hooks[:_MAX_HOOKS]):
        h["id"] = f"hook_{i + 1}"
        # Keep the crafted display line. Resetting text back to the spoken cue
        # here used to make downstream verbatim guards rewrite every hook into
        # generic template shells — the LLM's crafted hooks never shipped.
        text = " ".join(str(h.get("text") or "").split()).strip()
        spoken = " ".join(str(h.get("original_text") or text).split()).strip()
        if spoken:
            h["original_text"] = spoken
        if not text and spoken:
            h["text"] = spoken
            text = spoken
        h["verbatim"] = bool(text) and text == spoken
    base["hooks"] = all_hooks[:_MAX_HOOKS]
    base["topics"] = _flatten_topic_tree(topic_tree)[:_MAX_TOPICS]
    base["topic_tree"] = _reindex_topic_tree(topic_tree)[:_MAX_TOPICS]
    # Final structural proof: zero genuinely empty sections in the payload we
    # ship. A parent with populated children is intentionally allowed no own
    # hooks; copying a child hook here would violate tree uniqueness.
    empty_sections = _count_empty_hook_sections(base["topic_tree"])
    if empty_sections:
        logger.warning("pruning %d empty-hook sections after reindex", empty_sections)
        base["topic_tree"] = _drop_empty_hook_sections(base["topic_tree"])
        base["topics"] = _flatten_topic_tree(base["topic_tree"])[:_MAX_TOPICS]
        uniq = _hooks_from_topic_tree(base["topic_tree"])
        for i, h in enumerate(uniq[:_MAX_HOOKS]):
            h["id"] = f"hook_{i + 1}"
        base["hooks"] = uniq[:_MAX_HOOKS]
    multi_range = sum(
        1 for t in base["topic_tree"] if len(t.get("time_ranges") or []) >= 2
    )
    empty_after = _count_empty_hook_sections(base["topic_tree"])
    base["transcript_meta"] = {
        "cue_count": cue_count,
        "transcript_chars": cue_chars,
        "chunks_used": chunks_used,
        "topic_source": topic_source,
        "llm_provider": (
            llm_provider_used
            if llm_provider_used != "none"
            else (
                "claude"
                if llm_prefers_claude and has_llm
                else ("gemini" if has_llm else "none")
            )
        ),
        "claude_preferred": llm_prefers_claude,
        "topic_tree_count": len(base["topic_tree"]),
        "flat_topic_count": len(base["topics"]),
        "hook_count": len(base["hooks"]),
        "topics_with_multi_ranges": multi_range,
        "verbatim_guard": verbatim_stats_total,
        "empty_hook_sections": empty_after,
    }
    logger.info("carousel topic extract done: %s", base["transcript_meta"])

    base = await ensure_english_display_texts(
        base,
        english_cues=english_cues,
        api_key=api_key,
        model=model,
        claude_api_key=claude_api_key,
        claude_model=claude_model,
        provider=provider,
        openrouter_api_key=openrouter_api_key,
        openrouter_model=openrouter_model,
        openrouter_base_url=openrouter_base_url,
    )
    return base


async def _llm_craft_hooks(
    *,
    hooks: list[dict[str, Any]],
    theme_title: str,
    theme_summary: str,
    api_key: str | None = None,
    model: str = "",
    claude_api_key: str | None = None,
    claude_model: str = "",
    provider: str = "auto",
    openrouter_api_key: str | None = None,
    openrouter_model: str = "",
    openrouter_base_url: str = "",
) -> list[dict[str, Any]]:
    """Rewrite spoken windows into punchy carousel hook lines (keep time spans).

    Display text is analysed/crafted from the transcript — never a verbatim cue dump.
    start_sec / end_sec stay aligned to the original spoken span for frame selection.
    """
    if not hooks:
        return []

    payload = []
    for i, h in enumerate(hooks):
        payload.append(
            {
                "i": i,
                "spoken": str(h.get("text") or "").strip()[:400],
                "start_sec": h.get("start_sec"),
                "end_sec": h.get("end_sec"),
            }
        )

    prompt = (
        "You are an expert Instagram copywriter. Analyse spoken transcript windows and "
        "derive GENUINE, high-engagement HOOK lines for Instagram reels and 4:5 feed "
        "carousels.\n"
        f"{_HOOK_CRAFT_BRIEF}"
        "CRAFT RULES — hooks must be analysed, never pasted:\n"
        "- Derive a punchy hook FROM each spoken window — do NOT copy the transcript verbatim.\n"
        "- Rewrite into a complete, self-contained slide overlay (roughly 6–14 words).\n"
        "- ONE idea per hook; readable on a phone at ~48pt; no mid-clause scraps or cue dumps.\n"
        "- Prefer natural English; translate meaning if spoken line is Hindi/Hinglish/other.\n"
        "- Hooks must be DISTINCT from each other (no near-paraphrase twins); each hook "
        "needs its own shape and concrete nouns from THAT spoken window.\n"
        "- Return one hook per input index; keep the same order.\n"
        f"Theme: {theme_title}\n"
        f"Theme summary: {theme_summary}\n"
        "Return ONLY a JSON array of objects: "
        '{"i": number, "hook": "crafted English hook"}.\n\n'
        f"Spoken windows:\n{json.dumps(payload, ensure_ascii=False)}"
    )
    text, _provider = await _llm_complete_json(
        prompt=prompt,
        temperature=0.45,
        max_tokens=2200,
        api_key=api_key,
        model=model,
        claude_api_key=claude_api_key,
        claude_model=claude_model,
        provider=provider,
        openrouter_api_key=openrouter_api_key,
        openrouter_model=openrouter_model,
        openrouter_base_url=openrouter_base_url,
        json_root="array",
    )
    raw = _loads_json_array(text)
    if not raw:
        return []

    by_i: dict[int, str] = {}
    for row in raw:
        if not isinstance(row, dict):
            continue
        try:
            i = int(row.get("i"))
        except (TypeError, ValueError):
            continue
        hook = str(row.get("hook") or row.get("text") or "").strip()
        hook = " ".join(hook.split())
        if hook and 0 <= i < len(hooks):
            by_i[i] = hook[:280]

    if not by_i:
        return []

    out: list[dict[str, Any]] = []
    for i, h in enumerate(hooks):
        row = dict(h)
        crafted = by_i.get(i)
        if crafted:
            spoken = str(row.get("text") or "").strip()
            # If LLM echoed the transcript, fall back to a local punchy rewrite.
            if _nearly_verbatim(crafted, spoken) or not _hook_is_readable(crafted):
                crafted = (
                    _heuristic_hook_line(spoken, theme_title=theme_title)
                    or _force_non_verbatim_hook(spoken, theme_title=theme_title)
                    or _trim_to_clause(crafted, 16)
                )
            row["original_text"] = row.get("original_text") or spoken
            row["text"] = crafted
            row["verbatim"] = False
            row["analysed"] = True
            row["contextual"] = True
        out.append(row)
    return out


def keep_verbatim_transcript_hooks(
    hooks: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Keep spoken transcript text exactly — no rewrite, paraphrase, or punch-up.

    ``text`` and ``original_text`` are the same complete utterance so the UI can
    show hook ↔ transcript side by side without drift.
    """
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in hooks:
        row = dict(raw)
        spoken = " ".join(
            str(row.get("original_text") or row.get("text") or "").split()
        ).strip()
        if not spoken:
            continue
        key = spoken.lower()
        if key in seen:
            continue
        seen.add(key)
        row["text"] = spoken
        row["original_text"] = spoken
        row["verbatim"] = True
        row["english_source"] = row.get("english_source") or "indexed"
        out.append(row)
    return out


def heuristic_craft_hooks(
    hooks: list[dict[str, Any]],
    *,
    theme_title: str = "",
) -> list[dict[str, Any]]:
    """Local punchy rewrite when Gemini craft is unavailable — never ship raw cue dumps."""
    out: list[dict[str, Any]] = []
    corpus = [str(h.get("text") or "") for h in hooks if isinstance(h, dict)]
    used: set[str] = set()
    for i, h in enumerate(hooks):
        row = dict(h)
        spoken = str(row.get("text") or "").strip()
        if not spoken:
            continue
        crafted = _heuristic_hook_line(spoken, theme_title=theme_title)
        if (
            not crafted
            or is_verbatim_transcript_leak(crafted, [spoken, *corpus])
            or _hook_opening_collision(crafted, used)
        ):
            crafted = _force_non_verbatim_hook(
                spoken, theme_title=theme_title, used=used, salt=i
            )
        # Last resort still non-verbatim by construction.
        if not crafted or _hook_opening_collision(crafted, used):
            crafted = _force_non_verbatim_hook(
                spoken, theme_title=theme_title or "the talk", used=used, salt=i + 17
            )
        row["original_text"] = row.get("original_text") or spoken
        row["text"] = crafted
        row["verbatim"] = False
        row["analysed"] = True
        row["contextual"] = True
        out.append(row)
        used.add(" ".join(crafted.lower().split()))
    guarded, _stats = enforce_non_verbatim_hooks(out, corpus, theme_title=theme_title)
    return guarded


_HOOK_FILLER_WORDS = frozenset(
    {
        "like", "plus", "minus", "actually", "really", "just", "kinda", "sort",
        "think", "used", "point", "time", "first", "part", "entire", "okay",
        "yeah", "well", "know", "gonna", "wanna", "thing", "things", "stuff",
        "very", "quite", "maybe", "also", "still", "even", "much", "many",
        "some", "that", "this", "from", "have", "been", "they", "their",
        "about", "into", "what", "when", "where", "which", "while", "would",
        "could", "should", "there", "these", "those", "because", "after",
        "before", "people", "something", "everything", "quietly", "um", "uh",
    }
)
_MONEY_UNIT_RE = (
    r"(?:lakh|lakhs|crore|crores|cr|kore|kores"
    r"|billion|million|thousand|rupees|rs|dollars|percent)\b"
)
_MONEY_SPAN_RE = re.compile(
    rf"(?:(?:minus|plus|under|over|almost|nearly)\s+)?"
    rf"\d+(?:[.,]\d+)*(?:\s+\d+(?:[.,]\d+)*)?\s*{_MONEY_UNIT_RE}",
    re.I,
)


def _normalize_money_span(span: str) -> str:
    text = " ".join((span or "").split())
    text = re.sub(r"\bkores?\b", "crore", text, flags=re.I)
    text = re.sub(r"\bcrores\b", "crore", text, flags=re.I)
    text = re.sub(r"\blakhs\b", "lakh", text, flags=re.I)
    # Spoken "30 40 lakhs" means a range, not two separate amounts.
    text = re.sub(r"(\d+)\s+(\d+)\s+(lakh|crore|cr)\b", r"\1–\2 \3", text, flags=re.I)
    return text


def _money_spans_from_spoken(spoken: str) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for match in _MONEY_SPAN_RE.finditer(spoken or ""):
        span = _normalize_money_span(match.group(0))
        key = span.lower()
        if span and key not in seen:
            seen.add(key)
            out.append(span)
    return out


def _is_nouny_spark(spark: str) -> bool:
    tokens = re.findall(r"[A-Za-z0-9]+", (spark or "").lower())
    if not tokens:
        return False
    if any(ch.isdigit() for ch in spark):
        return True
    if any(token in _HOOK_FILLER_WORDS for token in tokens):
        return False
    return any(len(token) > 3 for token in tokens)


def _hook_spark_phrase(spoken: str, *, theme_title: str = "") -> tuple[str, str, str]:
    """Return (spark, spark_short, lead) that can sit inside a grammatical headline."""
    money = _money_spans_from_spoken(spoken)
    if money:
        spark = " to ".join(money[:2]) if len(money) >= 2 else money[0]
        if re.search(r"\bburn\b", spoken or "", flags=re.I):
            spark = f"{spark} burn"
        lead = money[0].split()[0]
        return spark, spark, lead

    stop = {
        "the", "and", "for", "with", "that", "this", "from", "have", "been",
        "they", "their", "about", "into", "every", "company", "largest",
        "people",
    } | _HOOK_FILLER_WORDS

    def _content(word: str) -> bool:
        return len(word) > 3 and word.lower() not in stop

    # Prefer a real noun phrase: two content words that sit next to each other
    # in the spoken sentence (e.g. "ghee business", "market value") instead of
    # disconnected words plucked from the middle of the window.
    raw_tokens = re.findall(r"[A-Za-z][A-Za-z0-9']+", spoken or "")
    bigram: list[str] = []
    for left, right in zip(raw_tokens, raw_tokens[1:]):
        if _content(left) and _content(right):
            bigram = [left, right]
            break
    words = [word for word in raw_tokens if _content(word)]
    if bigram:
        words = bigram + [w for w in words if w not in bigram][:2]
    elif len(words) > 4:
        mid = max(0, len(words) // 3)
        words = words[mid : mid + 4] or words[:4]
    else:
        words = words[:4]
    theme_bit = (theme_title or "this story").strip()[:48] or "this story"
    spark = " ".join(words[:3]).strip() if words else theme_bit
    spark_short = " ".join(words[:2]).strip() if words else theme_bit
    if not _is_nouny_spark(spark_short):
        spark_short = theme_bit
        spark = theme_bit
    lead = (words[0] if words else theme_bit).rstrip(".,;:")
    if lead.lower() in _HOOK_FILLER_WORDS:
        lead = theme_bit
    return spark, spark_short, lead


def _hook_is_readable(text: str) -> bool:
    """Reject filler-glued templates that are not readable English headlines."""
    words = re.findall(r"[A-Za-z0-9₹]+", text or "")
    if len(words) < 3:
        return False
    lower = [word.lower() for word in words]
    if "like" in lower:
        return False
    for left, right in zip(lower, lower[1:]):
        if left in {"like", "plus", "minus"} and right in {
            "like",
            "plus",
            "minus",
            "actually",
            "quietly",
            "still",
        }:
            return False
    content = [
        word
        for word in lower
        if word not in _HOOK_FILLER_WORDS and (len(word) > 2 or word.isdigit())
    ]
    return bool(content) or any(ch.isdigit() for ch in text)


def _heuristic_hook_line(spoken: str, *, theme_title: str = "") -> str:
    """Derive a short carousel hook from a spoken window without an LLM."""
    text = " ".join((spoken or "").split()).strip().strip("\"'")
    if not text:
        return ""
    money = _money_spans_from_spoken(text)
    if len(money) >= 2:
        line = f"From {money[0]} to {money[-1]}"
        if re.search(r"\bburn\b", text, flags=re.I):
            line = f"{money[0]} to {money[-1]} burn"
        return line[:280]
    if money:
        unit_line = f"The {money[0]} story"
        if re.search(r"\bmarket\b", text, flags=re.I):
            unit_line = f"A {money[0]} market, explained"
        if re.search(r"\bburn\b", text, flags=re.I):
            unit_line = f"The {money[0]} burn"
        return unit_line[:280]
    # Prefer first complete sentence / clause.
    m = re.search(r"^(.+?[.!?])(?:\s|$)", text)
    clause = (m.group(1) if m else text).strip()
    words = clause.split()
    if len(words) > 16:
        clause = _trim_to_clause(clause, 16)
        words = clause.split()
    # Drop filler openers that make dumps feel like captions.
    clause = re.sub(
        r"^(?:so|and|but|well|um+|uh+|you know|like|okay|ok|right)[,:\s]+",
        "",
        clause,
        flags=re.I,
    ).strip()
    if not clause:
        clause = " ".join(words[:12])
    if not _hook_is_readable(clause):
        return ""
    # Title-case lightly only when the line is a short claim.
    if 4 <= len(clause.split()) <= 10 and theme_title and theme_title.lower() in clause.lower():
        return clause[:280]
    if not clause[0:1].isupper() and clause:
        clause = clause[0].upper() + clause[1:]
    return clause[:280]


def _nearly_verbatim(crafted: str, spoken: str, *, threshold: float = 0.82) -> bool:
    """True when crafted text largely copies the spoken line token-for-token."""
    a = {t for t in re.findall(r"[a-z0-9']+", (crafted or "").lower()) if len(t) > 2}
    b = {t for t in re.findall(r"[a-z0-9']+", (spoken or "").lower()) if len(t) > 2}
    if not a or not b:
        return False
    overlap = len(a & b) / max(len(a), 1)
    return overlap >= threshold and abs(len(a) - len(b)) <= max(3, len(b) // 4)


def _hook_numbers_are_grounded(crafted: str, spoken: str) -> bool:
    """Reject numeric claims that do not occur in the supporting spoken window."""
    number_pattern = r"\d+(?:[.,]\d+)?"
    claimed = {n.replace(",", "") for n in re.findall(number_pattern, crafted or "")}
    supported = {n.replace(",", "") for n in re.findall(number_pattern, spoken or "")}
    return claimed <= supported


def _normalize_hook_cmp(text: str) -> str:
    """Lowercase alphanumerics only — for exact/near transcript matching."""
    return re.sub(r"[^a-z0-9]+", "", (text or "").lower())


def is_verbatim_transcript_leak(
    hook: str,
    corpus_texts: list[str],
    *,
    token_threshold: float = 0.78,
    lcs_ratio: float = 0.72,
) -> bool:
    """True if hook is exact/near-exact copy or long substring of any transcript cue/window."""
    from difflib import SequenceMatcher

    hook = " ".join((hook or "").split()).strip()
    if not hook:
        return True
    hn = _normalize_hook_cmp(hook)
    if len(hn) < 10:
        return False
    hw = {t for t in re.findall(r"[a-z0-9']+", hook.lower()) if len(t) > 2}
    for cue in corpus_texts:
        cue_s = " ".join((cue or "").split()).strip()
        if not cue_s:
            continue
        cn = _normalize_hook_cmp(cue_s)
        if not cn:
            continue
        # Exact or containment on normalized strings
        if hn == cn:
            return True
        if len(hn) >= 14 and (hn in cn or (len(cn) >= 14 and cn in hn)):
            return True
        # High token overlap with similar length (near-paraphrase dump)
        cw = {t for t in re.findall(r"[a-z0-9']+", cue_s.lower()) if len(t) > 2}
        if hw and cw:
            overlap = len(hw & cw) / max(len(hw), 1)
            if overlap >= token_threshold and abs(len(hw) - len(cw)) <= max(3, len(cw) // 4):
                return True
        # Long common substring on normalized forms
        if len(hn) >= 16 and len(cn) >= 16:
            lcs = SequenceMatcher(None, hn, cn).find_longest_match(0, len(hn), 0, len(cn)).size
            if lcs >= max(16, int(lcs_ratio * len(hn))):
                return True
    return False


def _force_non_verbatim_hook(
    spoken: str,
    *,
    theme_title: str = "",
    used: set[str] | None = None,
    salt: int = 0,
) -> str:
    """Aggressively rewrite a spoken window into a non-verbatim carousel claim.

    Must stay unique across a batch: never stamp the same stock opener
    (e.g. "The hidden pattern behind …") onto every hook.
    """
    spoken = " ".join((spoken or "").split()).strip()
    theme_bit = (theme_title or "this story").strip()[:48] or "this story"
    base = _heuristic_hook_line(spoken, theme_title=theme_title)
    if (
        base
        and _hook_is_readable(base)
        and not is_verbatim_transcript_leak(base, [spoken])
        and not _hook_opening_collision(base, used)
    ):
        return base

    spark, spark_short, lead = _hook_spark_phrase(spoken, theme_title=theme_bit)

    # Rotate templates by content so each spoken window gets a different shape.
    # Only wrap sparks that already read as a noun phrase. Keep these plain and
    # grammatical — clickbait shells glued around a random spark word shipped
    # nonsense like "food isn't the headline — it's the lever".
    templates = []
    if _is_nouny_spark(spark_short):
        templates.extend(
            [
                f"The story behind {spark_short}",
                f"{spark_short}, explained",
                f"The case for {spark_short}",
                f"A closer look at {spark_short}",
                f"Why {spark_short} matters",
                f"What changed with {spark_short}",
                f"How {spark_short} actually works",
                f"The real scale of {spark_short}",
            ]
        )
        if any(ch.isdigit() for ch in spoken):
            templates.append(f"The numbers behind {spark_short}")
    # Stable but varied pick from spoken content (+ salt for retries).
    seed = sum(ord(c) for c in (spoken.lower()[:80] or theme_bit)) + int(salt or 0)
    ordered = (
        [templates[(seed + i) % len(templates)] for i in range(len(templates))]
        if templates
        else []
    )
    # Theme-only fallbacks last (still unique via salt).
    ordered.extend(
        [
            f"The angle on {theme_bit} that sticks",
            f"What {theme_bit} gets wrong — and right",
            f"A sharper take on {theme_bit}",
        ]
    )

    for candidate in ordered:
        candidate = " ".join(candidate.split()).strip()[:280]
        if not candidate:
            continue
        if not _hook_is_readable(candidate):
            continue
        if is_verbatim_transcript_leak(candidate, [spoken]):
            continue
        if _hook_opening_collision(candidate, used):
            continue
        return candidate

    # Guaranteed unique divergence even if every template collided.
    suffix = abs(seed) % 97
    return f"A sharper take on {theme_bit} (#{suffix})"[:280]


def _banned_stock_opener(text: str) -> bool:
    """True for known boilerplate openers that stamped every hook identically."""
    head = " ".join((text or "").lower().split()[:4])
    banned = (
        "the hidden pattern behind",
        "what most people miss",
        "the real reason why",
        "the surprising truth about",
    )
    return any(head.startswith(b) or b in " ".join((text or "").lower().split()[:6]) for b in banned)


def _hook_opening_collision(text: str, used: set[str] | None) -> bool:
    """True when this hook shares a stock opening / near-duplicate with ``used``."""
    if _banned_stock_opener(text):
        return True
    if not used:
        return False
    norm = " ".join((text or "").lower().split())
    if not norm:
        return True
    if norm in used:
        return True
    # Same first 3–4 words = same boilerplate opener stamped on many hooks.
    head = " ".join(norm.split()[:4])
    for other in used:
        other_head = " ".join(other.split()[:4])
        if head and head == other_head:
            return True
        # Near-duplicate full lines
        if norm == other:
            return True
    return False


def enforce_non_verbatim_hooks(
    hooks: list[dict[str, Any]],
    corpus_texts: list[str],
    *,
    theme_title: str = "",
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Reject or rewrite hooks that leak transcript verbatim (LLM + heuristic paths)."""
    kept: list[dict[str, Any]] = []
    stats = {
        "checked": 0,
        "rejected_verbatim": 0,
        "rewritten": 0,
        "dropped": 0,
        "deduped_openings": 0,
    }
    corpus = [c for c in corpus_texts if (c or "").strip()]
    used_norms: set[str] = set()
    for h in hooks:
        if not isinstance(h, dict):
            continue
        stats["checked"] += 1
        text = str(h.get("text") or "").strip()
        spoken = str(h.get("original_text") or "").strip()
        local_corpus = list(corpus)
        if spoken and spoken != text:
            local_corpus.append(spoken)

        row = dict(h)
        if (
            text
            and _hook_is_readable(text)
            and not is_verbatim_transcript_leak(text, local_corpus)
        ):
            if _hook_opening_collision(text, used_norms):
                # Distinct topic hooks must not share the same stock opener.
                stats["deduped_openings"] += 1
                source = spoken or text
                rewritten = None
                for salt in range(0, 8):
                    cand = _force_non_verbatim_hook(
                        source, theme_title=theme_title, used=used_norms, salt=salt
                    )
                    if (
                        cand
                        and _hook_is_readable(cand)
                        and not _hook_opening_collision(cand, used_norms)
                    ):
                        rewritten = cand
                        break
                if rewritten:
                    row["original_text"] = source
                    row["text"] = rewritten
                    row["verbatim_guard"] = "opening_deduped"
                    text = rewritten
                else:
                    stats["dropped"] += 1
                    continue
            row["verbatim"] = False
            row["analysed"] = True
            kept.append(row)
            used_norms.add(" ".join(text.lower().split()))
            continue

        stats["rejected_verbatim"] += 1
        source = spoken or text
        rewritten = None
        for salt in range(0, 8):
            cand = _force_non_verbatim_hook(
                source, theme_title=theme_title, used=used_norms, salt=salt
            )
            if (
                cand
                and _hook_is_readable(cand)
                and not is_verbatim_transcript_leak(cand, local_corpus + [source])
                and not _hook_opening_collision(cand, used_norms)
            ):
                rewritten = cand
                break
        if rewritten:
            row["original_text"] = source
            row["text"] = rewritten
            row["verbatim"] = False
            row["analysed"] = True
            row["verbatim_guard"] = "rewritten"
            kept.append(row)
            used_norms.add(" ".join(rewritten.lower().split()))
            stats["rewritten"] += 1
            logger.info("verbatim guard rewrote hook: %r → %r", text[:70], rewritten[:70])
        else:
            stats["dropped"] += 1
            logger.info("verbatim guard dropped hook: %r", text[:90])
    return kept, stats


def _swap_hooks_with_english_cues(
    hooks: list[dict[str, Any]],
    english_cues: list[tuple[float, float | None, str]],
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for h in hooks:
        row = dict(h)
        if is_english_text(str(row.get("text") or "")):
            out.append(row)
            continue
        alt = english_text_for_window(
            english_cues,
            start_sec=float(row.get("start_sec") or 0),
            end_sec=row.get("end_sec"),
        )
        if alt:
            row["original_text"] = row.get("text")
            row["text"] = alt if len(alt.split()) <= 30 else _trim_to_clause(alt, 30)
            row["translated"] = False
            row["english_source"] = "caption_track"
            row["verbatim"] = True
        out.append(row)
    return out


async def ensure_english_display_texts(
    payload: dict[str, Any],
    *,
    english_cues: list[tuple[float, float | None, str]] | None = None,
    api_key: str | None,
    model: str,
    claude_api_key: str | None = None,
    claude_model: str = "",
    provider: str = "auto",
    openrouter_api_key: str | None = None,
    openrouter_model: str = "",
    openrouter_base_url: str = "",
) -> dict[str, Any]:
    """Translate remaining non-English hooks/topics to natural English for display."""
    hooks = [dict(h) for h in (payload.get("hooks") or [])]
    topics = [dict(t) for t in (payload.get("topics") or [])]

    # Last chance: map hooks to English cue windows before LLM translate.
    if english_cues:
        hooks = _swap_hooks_with_english_cues(hooks, english_cues)

    to_translate: list[tuple[str, int, str]] = []
    for i, h in enumerate(hooks):
        text = str(h.get("text") or "").strip()
        if text and needs_english(text):
            to_translate.append(("hook", i, text))
    for i, t in enumerate(topics):
        text = str(t.get("text") or "").strip()
        if text and needs_english(text):
            to_translate.append(("topic", i, text))

    translations: list[str] = []
    if to_translate and _llm_has_any_key(
        api_key=api_key,
        claude_api_key=claude_api_key,
        openrouter_api_key=openrouter_api_key,
        openrouter_model=openrouter_model,
    ):
        try:
            translations = await _llm_translate_lines(
                [text for _, _, text in to_translate],
                api_key=api_key,
                model=model,
                claude_api_key=claude_api_key,
                claude_model=claude_model,
                provider=provider,
                openrouter_api_key=openrouter_api_key,
                openrouter_model=openrouter_model,
                openrouter_base_url=openrouter_base_url,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("hook/topic English translation failed: %s", exc)

    any_translated = False
    for n, (kind, idx, original) in enumerate(to_translate):
        eng = translations[n] if n < len(translations) else ""
        eng = " ".join((eng or "").split()).strip()
        if not eng or not is_english_text(eng):
            continue
        if kind == "hook":
            hooks[idx]["original_text"] = hooks[idx].get("original_text") or original
            hooks[idx]["text"] = eng[:400]
            hooks[idx]["translated"] = True
            hooks[idx]["english_source"] = "llm_translate"
            hooks[idx]["verbatim"] = False
            any_translated = True
        else:
            topics[idx]["original_text"] = topics[idx].get("original_text") or original
            topics[idx]["text"] = eng[:120]
            topics[idx]["translated"] = True
            topics[idx]["english_source"] = "llm_translate"
            any_translated = True

    for h in hooks:
        h.setdefault("translated", False)
        h.setdefault("english_source", payload.get("english_source") or "indexed")
    for t in topics:
        t.setdefault("translated", False)
        t.setdefault("english_source", "generated")

    payload = dict(payload)
    payload["hooks"] = hooks
    payload["topics"] = topics
    payload["hooks_english"] = (
        all(is_english_text(str(h.get("text") or "")) for h in hooks) if hooks else True
    )
    payload["topics_english"] = (
        all(is_english_text(str(t.get("text") or "")) for t in topics) if topics else True
    )
    payload["any_translated"] = any_translated or any(bool(h.get("translated")) for h in hooks)
    return payload


async def _llm_translate_lines(
    lines: list[str],
    *,
    api_key: str | None = None,
    model: str = "",
    claude_api_key: str | None = None,
    claude_model: str = "",
    provider: str = "auto",
    openrouter_api_key: str | None = None,
    openrouter_model: str = "",
    openrouter_base_url: str = "",
) -> list[str]:
    """Translate lines to natural English; returns list aligned to input order."""
    if not lines:
        return []
    if not _llm_has_any_key(
        api_key=api_key,
        claude_api_key=claude_api_key,
        openrouter_api_key=openrouter_api_key,
        openrouter_model=openrouter_model,
    ):
        return [""] * len(lines)

    numbered = [{"i": i, "text": line} for i, line in enumerate(lines)]
    prompt = (
        "Translate each line into natural, spoken English for a video carousel hook/topic.\n"
        "Rules:\n"
        "- Preserve meaning; do NOT transliterate (no Romanized Hindi dumps).\n"
        "- Keep roughly the same length; complete sentences when the source is a sentence.\n"
        "- Return ONLY a JSON array of objects: {\"i\": number, \"text\": \"English\"}.\n\n"
        f"Lines:\n{json.dumps(numbered, ensure_ascii=False)}"
    )
    raw_text, _used = await _llm_complete_json(
        prompt=prompt,
        system="You faithfully translate spoken video captions. Return ONLY valid JSON.",
        temperature=0.2,
        max_tokens=2500,
        api_key=api_key,
        model=model,
        claude_api_key=claude_api_key,
        claude_model=claude_model,
        provider=provider,
        openrouter_api_key=openrouter_api_key,
        openrouter_model=openrouter_model,
        openrouter_base_url=openrouter_base_url,
        json_root="array",
    )
    raw = _loads_json_array(raw_text)
    out = [""] * len(lines)
    for row in raw:
        if not isinstance(row, dict):
            continue
        try:
            i = int(row.get("i"))
        except (TypeError, ValueError):
            continue
        text = str(row.get("text") or "").strip()
        if text and 0 <= i < len(lines):
            out[i] = text
    return out


# Back-compat alias
def extract_verbatim_hooks_topics(
    cues: list[tuple[float, float | None, str]],
    *,
    start_sec: float,
    end_sec: float | None,
) -> dict[str, Any]:
    return extract_hooks_and_topics(cues, start_sec=start_sec, end_sec=end_sec)


def _stitch_complete_utterances(
    window: list[tuple[float, float | None, str]],
) -> list[dict[str, Any]]:
    """Merge adjacent cues into complete thoughts (avoid incomplete / context-less scraps)."""
    chunks: list[dict[str, Any]] = []
    buf_text: list[str] = []
    buf_start: float | None = None
    buf_end: float | None = None

    def flush() -> None:
        nonlocal buf_text, buf_start, buf_end
        if not buf_text or buf_start is None:
            buf_text, buf_start, buf_end = [], None, None
            return
        text = " ".join(buf_text)
        text = re.sub(r"\s+", " ", text).strip()
        if len(text.split()) >= 4:
            chunks.append(
                {
                    "text": text,
                    "start_sec": float(buf_start),
                    "end_sec": float(buf_end) if buf_end is not None else None,
                }
            )
        buf_text, buf_start, buf_end = [], None, None

    for s, e, raw in window:
        piece = " ".join((raw or "").split())
        if not piece:
            continue
        if buf_start is None:
            buf_start = float(s)
        buf_text.append(piece)
        buf_end = float(e) if e is not None else float(s)
        joined = " ".join(buf_text)
        words = len(joined.split())
        ends_thought = bool(re.search(r"[.!?…][\"')\]]*$", piece)) or words >= 22
        if ends_thought and words >= 6:
            flush()
        elif words >= 36:
            flush()
    flush()
    return chunks


def _pick_contextual_hooks(stitched: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Prefer complete, self-contained spoken lines with enough context for a carousel card."""
    hooks: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in stitched:
        text = str(row.get("text") or "").strip()
        words = text.split()
        if len(words) < 6:
            continue
        # Drop obvious mid-clause scraps
        if text[:1].islower() and not re.match(r"^(I|I'm|I've|I'd|we|we're|you|it's)\b", text, re.I):
            continue
        key = text.lower()
        if key in seen:
            continue
        seen.add(key)
        # Keep the full spoken utterance — do not trim/rewrite for display.
        hooks.append(
            {
                "id": f"hook_{len(hooks) + 1}",
                "text": text,
                "original_text": text,
                "start_sec": float(row["start_sec"]),
                "end_sec": row.get("end_sec"),
                "verbatim": True,
                "contextual": True,
            }
        )
        if len(hooks) >= _MAX_HOOKS:
            break

    # If still thin, relax filters but merge more context
    if len(hooks) < 3:
        for row in stitched:
            text = str(row.get("text") or "").strip()
            if len(text.split()) < 5:
                continue
            key = text.lower()
            if key in {h["text"].lower() for h in hooks}:
                continue
            hooks.append(
                {
                    "id": f"hook_{len(hooks) + 1}",
                    "text": text if len(text.split()) <= 30 else _trim_to_clause(text, 30),
                    "start_sec": float(row["start_sec"]),
                    "end_sec": row.get("end_sec"),
                    "verbatim": True,
                    "contextual": True,
                }
            )
            if len(hooks) >= _MAX_HOOKS:
                break
    return hooks


_TOPIC_OVERLAP_STOP = frozenset(
    {
        "the", "a", "an", "and", "or", "to", "of", "in", "on", "for", "with", "is", "are",
        "that", "this", "it", "as", "at", "by", "from", "be", "you", "your", "have", "about",
    }
)


def _topic_text_overlap(topic_title: str, spoken: str) -> float:
    """How well a spoken candidate matches its topic label (content-token overlap)."""
    label_toks = {
        t
        for t in re.findall(r"[a-z0-9\u0900-\u097f]+", (topic_title or "").lower())
        if t not in _TOPIC_OVERLAP_STOP and len(t) > 2
    }
    snip_toks = {
        t
        for t in re.findall(r"[a-z0-9\u0900-\u097f]+", (spoken or "").lower())
        if t not in _TOPIC_OVERLAP_STOP and len(t) > 2
    }
    if not label_toks:
        return 0.0
    if not snip_toks:
        return 0.05
    hit = len(label_toks & snip_toks)
    return hit / len(label_toks) + (0.15 if hit else 0.0)


_BUSINESS_HOOK_TERMS = frozenset(
    {
        "acquisition", "business", "capital", "customer", "customers", "distribution",
        "entrepreneur", "entrepreneurship", "founder", "growth", "hiring", "leadership",
        "market", "marketing", "money", "operations", "pricing", "product", "profit",
        "profitability", "revenue", "sales", "scale", "startup", "strategy", "wealth",
    }
)


def _business_hook_score(text: str) -> float:
    """Favor verbatim lines that express a concrete business insight or promise."""
    clean = " ".join((text or "").split()).strip()
    if not clean:
        return 0.0
    lower = clean.lower()
    tokens = set(re.findall(r"[a-z0-9₹$%]+", lower))
    score = min(1.0, 0.25 * len(tokens & _BUSINESS_HOOK_TERMS))
    if re.search(r"(?:₹|\$|\b\d+(?:\.\d+)?\s*(?:cr|crore|lakh|million|billion|%))", lower):
        score += 0.5
    if re.search(r"\b(?:how|why|mistake|wrong|instead|secret|lesson|should|must|would)\b", lower):
        score += 0.35
    if re.search(r"\b(?:from zero|starting from|the real|most people|nobody|everyone)\b", lower):
        score += 0.25
    return min(score, 1.5)


def _spread_topic_spans(
    count: int,
    *,
    theme_start: float,
    theme_end: float | None,
    hooks: list[dict[str, Any]] | None = None,
    stitched: list[dict[str, Any]] | None = None,
) -> list[tuple[float, float | None]]:
    """Distinct time spans for topic labels — never stamp every topic at theme start."""
    n = max(0, int(count))
    if n == 0:
        return []

    refs: list[tuple[float, float | None]] = []
    for row in hooks or []:
        try:
            s = float(row.get("start_sec") or 0)
        except (TypeError, ValueError):
            continue
        end = row.get("end_sec")
        e = float(end) if end is not None else None
        if e is not None and e <= s:
            e = None
        refs.append((s, e))
    if not refs:
        for row in stitched or []:
            try:
                s = float(row.get("start_sec") or 0)
            except (TypeError, ValueError):
                continue
            end = row.get("end_sec")
            e = float(end) if end is not None else None
            if e is not None and e <= s:
                e = None
            refs.append((s, e))

    start = float(theme_start or 0)
    end = float(theme_end) if theme_end is not None else None
    if end is None or end <= start:
        if refs:
            last_e = max((e if e is not None else s + 4.0) for s, e in refs)
            end = max(last_e, start + max(4.0, n * 4.0))
        else:
            end = start + max(8.0, n * 5.0)

    if refs and len(refs) >= n:
        step = len(refs) / n
        return [refs[min(len(refs) - 1, int(i * step))] for i in range(n)]

    window = max(end - start, float(n))
    seg = window / n
    spans: list[tuple[float, float | None]] = []
    for i in range(n):
        s = round(start + i * seg, 2)
        e = round(start + (i + 1) * seg, 2)
        if refs and i < len(refs):
            hs, he = refs[i]
            s = hs
            e = float(he) if he is not None else round(hs + max(3.0, seg), 2)
        if e <= s:
            e = round(s + max(3.0, seg), 2)
        spans.append((s, e))
    return spans


def _topics_from_theme(
    *,
    theme_title: str,
    theme_summary: str,
    hooks: list[dict[str, Any]] | list[str],
    stitched: list[dict[str, Any]],
    theme_start: float = 0.0,
    theme_end: float | None = None,
) -> list[dict[str, Any]]:
    """Heuristic thematic topics from the selected theme (labels, not transcript dumps)."""
    title = (theme_title or "").strip() or "Theme"
    summary = (theme_summary or "").strip()
    hook_rows: list[dict[str, Any]] = []
    hook_texts: list[str] = []
    for h in hooks or []:
        if isinstance(h, dict):
            hook_rows.append(h)
            hook_texts.append(str(h.get("text") or ""))
        else:
            hook_texts.append(str(h))

    seeds: list[str] = []
    if title:
        seeds.append(title)
    for part in re.split(r"[.;\n]", summary):
        cleaned = " ".join(part.split()).strip(" -–—")
        if 3 <= len(cleaned.split()) <= 12:
            seeds.append(cleaned)
    for h in hook_texts[:4]:
        angle = _topic_angle_from_hook(h)
        if angle:
            seeds.append(angle)

    labels: list[str] = []
    seen: set[str] = set()
    for label in seeds:
        key = label.lower()
        if key in seen or len(label) < 4:
            continue
        seen.add(key)
        if len(label.split()) > 14 or (label.startswith('"') or label.count(",") > 2):
            continue
        labels.append(label[:120])
        if len(labels) >= _MAX_TOPICS:
            break

    if not labels:
        labels = [title[:120]]

    spans = _spread_topic_spans(
        len(labels),
        theme_start=theme_start,
        theme_end=theme_end,
        hooks=hook_rows,
        stitched=stitched,
    )
    topics: list[dict[str, Any]] = []
    for i, label in enumerate(labels):
        s, e = spans[i] if i < len(spans) else (float(theme_start or 0), None)
        topics.append(
            {
                "id": f"topic_{i + 1}",
                "text": label,
                "start_sec": float(s),
                "end_sec": float(e) if e is not None else None,
                "verbatim": False,
                "generated": True,
            }
        )
    return topics


def _expand_thin_topic_window(
    all_cues: list[tuple[float, float | None, str]],
    window: list[tuple[float, float | None, str]],
    *,
    start_sec: float,
    end_sec: float | None,
    min_cues: int = 16,
    min_chars: int = 1_200,
) -> tuple[list[tuple[float, float | None, str]], float, float | None]:
    """Grow a catastrophically thin theme window so topic extraction can read the talk.

    Only expands when the slice is nearly empty (bad timestamps / sparse English track).
    Healthy theme windows are left unchanged so topics stay on-theme.
    """
    usable = [(s, e, t) for s, e, t in all_cues if (t or "").strip()]
    if not usable:
        return window, start_sec, end_sec

    def _chars(rows: list[tuple[float, float | None, str]]) -> int:
        return len(compact_transcript(rows, max_chars=200_000))

    cur = list(window) if window else _cues_in_range(usable, start_sec, end_sec)
    cur_start = float(start_sec or 0)
    cur_end = float(end_sec) if end_sec is not None else None
    if len(cur) >= min_cues and _chars(cur) >= min_chars:
        return cur, cur_start, cur_end

    # Index bounds in the full cue list
    starts = [float(s) for s, _, _ in usable]
    # Find leftmost cue index at/after theme start
    lo = 0
    for i, s in enumerate(starts):
        if s >= cur_start - 0.05:
            lo = i
            break
    hi = len(usable) - 1
    if cur_end is not None:
        for i, s in enumerate(starts):
            if s <= float(cur_end) + 0.25:
                hi = i
            else:
                break
    # Expand outward until thresholds met or we hit video edges
    while (hi - lo + 1) < len(usable) and (
        (hi - lo + 1) < min_cues or _chars(usable[lo : hi + 1]) < min_chars
    ):
        grew = False
        if hi + 1 < len(usable):
            hi += 1
            grew = True
        if (hi - lo + 1) < min_cues or _chars(usable[lo : hi + 1]) < min_chars:
            if lo > 0:
                lo -= 1
                grew = True
        if not grew:
            break

    expanded = usable[lo : hi + 1]
    new_start = float(expanded[0][0]) if expanded else cur_start
    new_end = (
        float(expanded[-1][1] if expanded[-1][1] is not None else expanded[-1][0])
        if expanded
        else cur_end
    )
    logger.info(
        "expanded thin topic window: cues %d→%d chars %d→%d span %.1f–%s → %.1f–%.1f",
        len(window),
        len(expanded),
        _chars(window),
        _chars(expanded),
        cur_start,
        f"{cur_end:.1f}" if cur_end is not None else "end",
        new_start,
        new_end,
    )
    return expanded, new_start, new_end


def _chunk_cues_for_topics(
    cues: list[tuple[float, float | None, str]],
    *,
    max_chars: int = _TOPIC_CHUNK_CHARS,
    overlap_cues: int = _TOPIC_CHUNK_OVERLAP_CUES,
) -> list[list[tuple[float, float | None, str]]]:
    """Split timed cues into overlapping chunks so Gemini can read the full talk."""
    usable = [(s, e, t) for s, e, t in cues if (t or "").strip()]
    if not usable:
        return []
    chunks: list[list[tuple[float, float | None, str]]] = []
    current: list[tuple[float, float | None, str]] = []
    total = 0
    for cue in usable:
        line = format_cue_line(cue[0], cue[1], cue[2])
        line_len = len(line) + 1
        if current and total + line_len > max_chars:
            chunks.append(current)
            overlap = current[-overlap_cues:] if overlap_cues > 0 else []
            current = list(overlap)
            total = 0
            for oc in current:
                total += len(format_cue_line(oc[0], oc[1], oc[2])) + 1
        current.append(cue)
        total += line_len
    if current:
        chunks.append(current)
    return chunks


def _merge_topic_trees(parts: list[list[dict[str, Any]]]) -> list[dict[str, Any]]:
    """Merge chunk topic trees chronologically; drop only near-duplicate titles."""
    merged: list[dict[str, Any]] = []
    for part in parts:
        merged.extend(part)
    merged.sort(key=lambda t: float(t.get("start_sec") or 0))
    kept: list[dict[str, Any]] = []
    kept_tokens: list[set[str]] = []
    for row in merged:
        text = str(row.get("text") or "").strip()
        if not text:
            continue
        toks = _token_set(text)
        if any(_jaccard(toks, existing) >= 0.62 for existing in kept_tokens):
            continue
        kept.append(row)
        kept_tokens.append(toks)
        if len(kept) >= _MAX_TOPICS:
            break
    return kept


def _normalize_topic_chronology(
    topics: list[dict[str, Any]],
    *,
    span_start: float = 0.0,
    span_end: float | None = None,
) -> list[dict[str, Any]]:
    """Force topics (and their subtopics) into one clean, sequential, non-overlapping timeline.

    Every topic gets exactly one real chronological window — no enveloping
    multi-range spans and no missing/incomplete end times — so the UI can show
    a simple, trustworthy timeline instead of siblings that appear to overlap.
    """
    if not topics:
        return topics
    ordered = sorted(
        (t for t in topics if isinstance(t, dict)),
        key=lambda t: float(_as_float(t.get("start_sec")) or 0.0),
    )
    n = len(ordered)
    for i, t in enumerate(ordered):
        t.pop("time_ranges", None)
        start = float(_as_float(t.get("start_sec")) or 0.0)
        start = max(start, span_start)
        next_start = (
            float(_as_float(ordered[i + 1].get("start_sec")) or start)
            if i + 1 < n
            else None
        )
        end = _as_float(t.get("end_sec"))
        if end is None or end <= start:
            end = next_start if next_start is not None else (
                span_end if span_end is not None else start + 20.0
            )
        if next_start is not None and end > next_start:
            end = next_start
        if end <= start:
            end = start + 1.0
        t["start_sec"] = start
        t["end_sec"] = end

        subs_raw = t.get("subtopics")
        subs = sorted(
            (s for s in subs_raw if isinstance(s, dict)) if isinstance(subs_raw, list) else [],
            key=lambda s: float(_as_float(s.get("start_sec")) or start),
        )
        m = len(subs)
        for j, s in enumerate(subs):
            s_start = max(start, min(float(_as_float(s.get("start_sec")) or start), end))
            s_next = (
                max(start, min(float(_as_float(subs[j + 1].get("start_sec")) or s_start), end))
                if j + 1 < m
                else end
            )
            s_end = _as_float(s.get("end_sec"))
            if s_end is None or s_end <= s_start:
                s_end = s_next
            s_end = min(float(s_end), end)
            if s_end <= s_start:
                s_end = min(end, s_start + 1.0)
            s["start_sec"] = s_start
            s["end_sec"] = s_end
        t["subtopics"] = subs
    return ordered


def _cue_is_theme_filler(text: str) -> bool:
    cleaned = " ".join((text or "").split())
    if not cleaned:
        return True
    if _THEME_FILLER_CUE_RE.match(cleaned):
        return True
    letters = sum(1 for ch in cleaned if ch.isalpha())
    return letters < 10


def _condense_transcript_outline(
    cues: list[tuple[float, float | None, str]],
    *,
    max_chars: int = 9_000,
) -> str:
    """Sample a substance-dense timed outline for global theme synthesis.

    Drops music/applause/near-empty cues, then evenly samples the remaining
    talk so long videos keep start → middle → end coverage inside ``max_chars``.
    """
    raw = [(s, e, t) for s, e, t in cues if (t or "").strip()]
    if not raw:
        return ""
    usable = [(s, e, t) for s, e, t in raw if not _cue_is_theme_filler(t)]
    if len(usable) < 2:
        usable = raw
    full_chars = sum(
        len(format_cue_line(start, end, text)) + 1
        for start, end, text in usable
    )
    if full_chars <= max_chars:
        return compact_transcript(usable, max_chars=max_chars)
    average_line_chars = max(1, full_chars // len(usable))
    target_lines = max(2, min(len(usable), max_chars // average_line_chars))
    while target_lines >= 2:
        indices = sorted(
            {
                round(i * (len(usable) - 1) / (target_lines - 1))
                for i in range(target_lines)
            }
        )
        sampled = [usable[i] for i in indices]
        outline = compact_transcript(sampled, max_chars=max_chars)
        last_line = format_cue_line(*usable[-1])
        if last_line in outline:
            return outline
        target_lines -= 1
    return compact_transcript([usable[0], usable[-1]], max_chars=max_chars)


def _cues_for_topic_ranges(
    cues: list[tuple[float, float | None, str]],
    topic: dict[str, Any],
    *,
    fallback_start: float,
    fallback_end: float | None,
) -> list[tuple[float, float | None, str]]:
    """Gather cues for a topic, supporting multiple non-contiguous time_ranges."""
    ranges = topic.get("time_ranges") if isinstance(topic.get("time_ranges"), list) else []
    collected: list[tuple[float, float | None, str]] = []
    seen_starts: set[float] = set()
    for raw in ranges:
        if not isinstance(raw, dict):
            continue
        rs = _as_float(raw.get("start_sec", raw.get("start")))
        re_ = _as_float(raw.get("end_sec", raw.get("end")))
        if rs is None:
            continue
        for cue in _cues_in_range(cues, float(rs), re_):
            key = float(cue[0])
            if key in seen_starts:
                continue
            seen_starts.add(key)
            collected.append(cue)
    if collected:
        collected.sort(key=lambda c: float(c[0]))
        return collected
    start = _as_float(topic.get("start_sec"))
    end = _as_float(topic.get("end_sec"))
    return _cues_in_range(
        cues,
        float(start if start is not None else fallback_start),
        end if end is not None else fallback_end,
    )


async def _llm_synthesize_global_topics(
    *,
    candidates: list[dict[str, Any]],
    outline: str,
    theme_title: str,
    theme_summary: str,
    search_entity: str | None,
    api_key: str | None = None,
    model: str = "",
    claude_api_key: str | None = None,
    claude_model: str = "",
    provider: str = "auto",
    openrouter_api_key: str | None = None,
    openrouter_model: str = "",
    openrouter_base_url: str = "",
    theme_start: float,
    theme_end: float | None,
) -> tuple[list[dict[str, Any]], str]:
    """Global pass: turn fragmentary chunk topics into cohesive transcript-spanning threads."""
    if not outline.strip() or not candidates:
        return candidates, "none"

    cand_payload = []
    for c in candidates[:40]:
        cand_payload.append(
            {
                "title": c.get("text"),
                "start_sec": c.get("start_sec"),
                "end_sec": c.get("end_sec"),
                "explanation": c.get("explanation"),
                "subtopics": [
                    {
                        "title": s.get("text"),
                        "start_sec": s.get("start_sec"),
                        "end_sec": s.get("end_sec"),
                        "explanation": s.get("explanation"),
                    }
                    for s in (c.get("subtopics") or [])[:4]
                    if isinstance(s, dict)
                ],
            }
        )

    entity = (search_entity or "").strip()
    prompt = (
        "You synthesize COHESIVE, transcript-spanning TOPICS for a video carousel.\n"
        "You are given (1) a condensed timed outline of the FULL talk and (2) candidate "
        "local topics extracted from chunks (these may be fragmentary single-moment labels).\n\n"
        "Goal: produce parent topics that are COHERENT THREADS across the talk — narrative arcs, "
        "recurring ideas, and how the speaker's direction evolves — NOT isolated moment tags.\n\n"
        "Business lens: surface commercially useful ideas even when they are expressed through "
        "stories or informal language—customer acquisition, distribution, product, pricing, sales, "
        "growth, capital, profitability, operations, hiring, leadership, market strategy, founder "
        "mistakes, wealth creation, and policy effects on enterprise. Prefer the speaker's concrete "
        "thesis, mechanism, playbook, trade-off, or outcome over a generic category label.\n\n"
        "Hard rules:\n"
        "- Output 5–12 parent topics (keep good granularity; do NOT collapse to 2–3 vague umbrellas)\n"
        "- Each topic = one coherent thread that can recur; use time_ranges for EVERY span where "
        "that thread appears (non-contiguous ranges are encouraged when the speaker returns to it)\n"
        "- Also set start_sec / end_sec to the earliest start and latest end across its ranges\n"
        "- subtopics: 1–4 local direction-shift beats nested under the parent thread "
        "(reuse/refine chunk candidates where they fit)\n"
        "- Titles: 2–8 words, concrete, grounded in the outline — not generic chapter names\n"
        "- explanation: 1 sentence on how this thread develops across the talk\n"
        "- Prefer merging duplicate local candidates into one spanning thread\n"
        "- Use editorial creativity to select and phrase the strongest business-relevant topics, "
        "but do NOT invent facts, numbers, promises, or lessons beyond the outline/candidates\n"
        f"Theme title: {theme_title}\n"
        f"Theme summary: {theme_summary}\n"
        f"Window: {theme_start}s – {theme_end if theme_end is not None else 'end'}s\n"
        f"Search entity: {entity or '(none)'}\n"
        "Return ONLY a JSON array of objects:\n"
        '{"title":"...","start_sec":0,"end_sec":10,'
        '"time_ranges":[{"start_sec":0,"end_sec":20},{"start_sec":120,"end_sec":150}],'
        '"explanation":"...",'
        '"subtopics":[{"title":"...","start_sec":0,"end_sec":10,"explanation":"..."}]}\n\n'
        f"Candidate local topics:\n{json.dumps(cand_payload, ensure_ascii=False)}\n\n"
        f"Condensed full-talk outline (READ FOR GLOBAL ARC):\n{outline}"
    )
    text, provider = await _llm_complete_json(
        prompt=prompt,
        temperature=0.3,
        max_tokens=4096,
        api_key=api_key,
        model=model,
        claude_api_key=claude_api_key,
        claude_model=claude_model,
        provider=provider,
        openrouter_api_key=openrouter_api_key,
        openrouter_model=openrouter_model,
        openrouter_base_url=openrouter_base_url,
    )
    synthesized = _parse_topic_tree_json(
        text,
        theme_start=theme_start,
        theme_end=theme_end,
    )
    return (synthesized if synthesized else candidates), provider


async def _llm_topic_tree_from_cues(
    *,
    cues: list[tuple[float, float | None, str]],
    theme_title: str,
    theme_summary: str,
    search_entity: str | None,
    api_key: str | None = None,
    model: str = "",
    claude_api_key: str | None = None,
    claude_model: str = "",
    provider: str = "auto",
    openrouter_api_key: str | None = None,
    openrouter_model: str = "",
    openrouter_base_url: str = "",
    theme_start: float = 0.0,
    theme_end: float | None = None,
) -> tuple[list[dict[str, Any]], int, str]:
    """Chunk-read the talk, then globally synthesize cohesive spanning topics.

    Returns ``(topics, chunks_used, provider)`` where provider is ``claude``,
    ``gemini``, or ``none``.
    """
    chunks = _chunk_cues_for_topics(cues)
    if not chunks:
        return [], 0, "none"
    parts: list[list[dict[str, Any]]] = []
    providers: list[str] = []
    for idx, chunk in enumerate(chunks):
        transcript = compact_transcript(chunk, max_chars=_TOPIC_CHUNK_CHARS + 2_000)
        c_start = float(chunk[0][0])
        c_end_raw = chunk[-1][1] if chunk[-1][1] is not None else chunk[-1][0]
        c_end = float(c_end_raw)
        logger.info(
            "carousel topic chunk %d/%d: cues=%d chars=%d span=%.1f–%.1f",
            idx + 1,
            len(chunks),
            len(chunk),
            len(transcript),
            c_start,
            c_end,
        )
        try:
            part, used_provider = await _llm_topic_tree_from_theme(
                theme_title=theme_title,
                theme_summary=theme_summary,
                transcript=transcript,
                search_entity=search_entity,
                api_key=api_key,
                model=model,
                claude_api_key=claude_api_key,
                claude_model=claude_model,
                provider=provider,
                openrouter_api_key=openrouter_api_key,
                openrouter_model=openrouter_model,
                openrouter_base_url=openrouter_base_url,
                theme_start=c_start,
                theme_end=c_end,
                chunk_index=idx,
                chunk_count=len(chunks),
            )
        except Exception as exc:  # noqa: BLE001
            # A long talk must not lose every chunk because one request failed.
            logger.warning(
                "carousel topic chunk %d/%d failed: %s", idx + 1, len(chunks), exc
            )
            continue
        if part:
            parts.append(part)
            if used_provider:
                providers.append(used_provider)
    if not parts:
        return [], len(chunks), "none"

    candidates = parts[0] if len(parts) == 1 else _merge_topic_trees(parts)
    if "openrouter" in providers:
        used = "openrouter"
    elif "claude" in providers:
        used = "claude"
    else:
        used = providers[-1] if providers else "none"
    # No global "cohesive thread" re-synthesis: that pass enveloped each topic's
    # start/end across every non-contiguous recurrence, which made sibling topics
    # look like they overlapped in time. Chunk-level candidates are already
    # chronological and single-span; normalize just clips real overlaps/gaps.
    normalized = _normalize_topic_chronology(
        candidates[:_MAX_TOPICS],
        span_start=theme_start,
        span_end=theme_end,
    )
    return normalized, len(chunks), used


async def _llm_topic_tree_from_theme(
    *,
    theme_title: str,
    theme_summary: str,
    transcript: str,
    search_entity: str | None,
    api_key: str | None = None,
    model: str = "",
    claude_api_key: str | None = None,
    claude_model: str = "",
    provider: str = "auto",
    openrouter_api_key: str | None = None,
    openrouter_model: str = "",
    openrouter_base_url: str = "",
    theme_start: float = 0.0,
    theme_end: float | None = None,
    chunk_index: int = 0,
    chunk_count: int = 1,
) -> tuple[list[dict[str, Any]], str]:
    """Infer cohesive topic clusters (+ optional subtopics) from one transcript chunk."""
    entity = (search_entity or "").strip()
    chunk_note = (
        f"This is chunk {chunk_index + 1} of {chunk_count} of the same talk. "
        "Extract EVERY distinct direction shift in THIS chunk — do not summarize the whole video "
        "into a handful of vague themes.\n"
        if chunk_count > 1
        else ""
    )
    prompt = (
        "You carefully READ the timed transcript below and extract COHESIVE TOPICS.\n"
        "A cohesive topic = a real thematic cluster where the speaker takes a clear direction "
        "or develops one idea for a stretch of time (a narrative chapter), NOT keyword tags, "
        "NOT scattered buzzwords, NOT one vague umbrella for the whole talk.\n\n"
        f"{chunk_note}"
        "These are LOCAL candidate beats for a later global synthesis pass. Still be specific.\n"
        "Flow: Topics → optional Subtopics (no hooks here).\n"
        "Business lens: actively detect concrete entrepreneurial ideas, including acquisition, "
        "distribution, product, pricing, sales, growth, capital, profit, operations, hiring, "
        "leadership, market strategy, wealth creation, policy impact on enterprise, founder "
        "mistakes, contrarian lessons, and actionable playbooks—even when the speaker uses "
        "informal language rather than business jargon.\n"
        "Hard rules:\n"
        "- Extract MORE candidates when the speaker pivots: aim for 5–10 top-level topics in this "
        "chunk when the talk supports it (minimum 4 if the chunk is substantive). "
        "Do NOT collapse a long discussion into 2–3 generic labels.\n"
        "- Follow the transcript chronologically; each topic must map to real cue timestamps\n"
        "- Each topic title: 2–8 words, natural English, ONE concrete idea from what was said. "
        "Prefer the actual insight, strategy, mistake, trade-off, or outcome over a category label\n"
        "- start_sec / end_sec must cover that direction using the cue times in the transcript\n"
        "- explanation: 1 sentence grounded in the spoken content (what they actually develop)\n"
        "- subtopics: 0–3 nested beats ONLY when the speaker subdivides the same direction\n"
        "- Distinct topics only (no near-duplicate labels), but keep adjacent distinct angles\n"
        "- READ the lines — titles must reflect specific claims/stories in the transcript, "
        "not invent generic chapter names that could fit any video\n"
        "- Use editorial creativity to make titles compelling and useful to entrepreneurs, but "
        "do NOT invent claims, numbers, promises, or facts beyond the transcript\n"
        f"Theme title: {theme_title}\n"
        f"Theme summary: {theme_summary}\n"
        f"Chunk window: {theme_start}s – {theme_end if theme_end is not None else 'end'}s\n"
        f"Search entity: {entity or '(none)'}\n"
        "Return ONLY a JSON array of objects:\n"
        '{"title":"...","start_sec":0,"end_sec":10,"explanation":"...",'
        '"subtopics":[{"title":"...","start_sec":0,"end_sec":5,"explanation":"..."}]}\n\n'
        f"Timed transcript (READ ALL OF IT):\n{transcript}"
    )
    text, provider = await _llm_complete_json(
        prompt=prompt,
        temperature=0.35,
        max_tokens=4096,
        api_key=api_key,
        model=model,
        claude_api_key=claude_api_key,
        claude_model=claude_model,
        provider=provider,
        openrouter_api_key=openrouter_api_key,
        openrouter_model=openrouter_model,
        openrouter_base_url=openrouter_base_url,
    )
    return (
        _parse_topic_tree_json(
            text,
            theme_start=theme_start,
            theme_end=theme_end,
        ),
        provider,
    )


def _parse_topic_tree_json(
    text: str,
    *,
    theme_start: float,
    theme_end: float | None,
) -> list[dict[str, Any]]:
    m = re.search(r"\[[\s\S]*\]", text or "")
    if not m:
        return []
    try:
        raw = json.loads(m.group())
    except json.JSONDecodeError:
        return []
    if not isinstance(raw, list):
        return []
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for i, row in enumerate(raw):
        if not isinstance(row, dict):
            continue
        title = str(row.get("title") or row.get("text") or row.get("topic") or "").strip()
        if not title or len(title.split()) > 12:
            continue
        key = title.lower()
        if key in seen:
            continue
        seen.add(key)
        start = _as_float(row.get("start_sec", row.get("start")))
        end = _as_float(row.get("end_sec", row.get("end")))
        time_ranges: list[dict[str, float]] = []
        raw_ranges = row.get("time_ranges") if isinstance(row.get("time_ranges"), list) else []
        for tr in raw_ranges[:8]:
            if not isinstance(tr, dict):
                continue
            rs = _as_float(tr.get("start_sec", tr.get("start")))
            re_ = _as_float(tr.get("end_sec", tr.get("end")))
            if rs is None:
                continue
            time_ranges.append(
                {
                    "start_sec": float(rs),
                    "end_sec": float(re_) if re_ is not None else float(rs),
                }
            )
        if time_ranges:
            start = min(r["start_sec"] for r in time_ranges)
            end = max(r["end_sec"] for r in time_ranges)
        if start is None:
            start = float(theme_start or 0)
        if end is None and theme_end is not None:
            end = float(theme_end)
        if not time_ranges and start is not None:
            time_ranges = [
                {
                    "start_sec": float(start),
                    "end_sec": float(end) if end is not None else float(start),
                }
            ]
        expl = str(row.get("explanation") or row.get("summary") or "").strip()[:400]
        subs_raw = row.get("subtopics") if isinstance(row.get("subtopics"), list) else []
        subtopics: list[dict[str, Any]] = []
        sub_seen: set[str] = set()
        for j, sub in enumerate(subs_raw[:4]):
            if not isinstance(sub, dict):
                continue
            st = str(sub.get("title") or sub.get("text") or "").strip()
            if not st or st.lower() in sub_seen or st.lower() == key:
                continue
            sub_seen.add(st.lower())
            ss = _as_float(sub.get("start_sec", sub.get("start")))
            se = _as_float(sub.get("end_sec", sub.get("end")))
            subtopics.append(
                {
                    "id": f"topic_{i + 1}_sub_{j + 1}",
                    "text": st[:120],
                    "start_sec": float(ss if ss is not None else start),
                    "end_sec": float(se) if se is not None else (float(end) if end is not None else None),
                    "explanation": str(sub.get("explanation") or "").strip()[:300],
                    "hooks": [],
                }
            )
        out.append(
            {
                "id": f"topic_{i + 1}",
                "text": title[:120],
                "start_sec": float(start),
                "end_sec": float(end) if end is not None else None,
                "time_ranges": time_ranges,
                "explanation": expl,
                "verbatim": False,
                "generated": True,
                "subtopics": subtopics,
                "hooks": [],
            }
        )
        if len(out) >= _MAX_TOPICS:
            break
    return out


def _flat_topics_to_tree(topics: list[dict[str, Any]]) -> list[dict[str, Any]]:
    tree: list[dict[str, Any]] = []
    for i, t in enumerate(topics[:_MAX_TOPICS]):
        if not isinstance(t, dict):
            continue
        text = str(t.get("text") or "").strip()
        if not text:
            continue
        tree.append(
            {
                "id": t.get("id") or f"topic_{i + 1}",
                "text": text,
                "start_sec": float(t.get("start_sec") or 0),
                "end_sec": t.get("end_sec"),
                "explanation": str(t.get("explanation") or ""),
                "verbatim": False,
                "generated": True,
                "subtopics": [],
                "hooks": [],
            }
        )
    return tree


def _emergency_hook_candidates(
    cues_or_stitched: list[Any],
    *,
    limit: int = 3,
) -> list[dict[str, Any]]:
    """Last-resort hook seeds from the longest spoken lines in a window."""
    rows: list[dict[str, Any]] = []
    for item in cues_or_stitched or []:
        if isinstance(item, dict):
            text = str(item.get("text") or "").strip()
            start = float(item.get("start_sec") or 0)
            end = item.get("end_sec")
        elif isinstance(item, (tuple, list)) and len(item) >= 3:
            start = float(item[0] or 0)
            end = item[1]
            text = str(item[2] or "").strip()
        else:
            continue
        if len(text.split()) < 4:
            continue
        rows.append(
            {
                "id": f"emerg_{len(rows) + 1}",
                "text": text if len(text.split()) <= 32 else _trim_to_clause(text, 32),
                "start_sec": start,
                "end_sec": end,
                "verbatim": True,
                "contextual": True,
            }
        )
    rows.sort(key=lambda r: len(str(r.get("text") or "").split()), reverse=True)
    return rows[:limit]


def _count_empty_hook_sections(tree: list[dict[str, Any]]) -> int:
    n = 0
    for t in tree or []:
        if not list(t.get("hooks") or []) and not any(
            list(s.get("hooks") or []) for s in (t.get("subtopics") or [])
        ):
            n += 1
        for sub in t.get("subtopics") or []:
            if not list(sub.get("hooks") or []):
                n += 1
    return n


def _drop_empty_hook_sections(tree: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Remove sections with no hooks anywhere, preserving child-only parents."""
    out: list[dict[str, Any]] = []
    for t in tree or []:
        row = dict(t)
        subs = []
        for sub in row.get("subtopics") or []:
            if list(sub.get("hooks") or []):
                subs.append(sub)
        row["subtopics"] = subs
        parent_hooks = list(row.get("hooks") or [])
        if not parent_hooks and not subs:
            continue
        if parent_hooks or subs:
            out.append(row)
    return out


def _ensure_hooks_on_every_section(
    topic_tree: list[dict[str, Any]],
    *,
    all_hooks: list[dict[str, Any]],
    cues: list[Any],
    theme_title: str,
    cue_corpus: list[str],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, int]]:
    """Backfill or drop sections with no content without copying hooks."""
    stats = {"pruned": 0, "backfilled": 0}
    kept: list[dict[str, Any]] = []
    hooks_out = list(all_hooks)

    for topic in topic_tree:
        row = dict(topic)
        topic_hooks = list(row.get("hooks") or [])
        if not topic_hooks:
            window = _cues_for_topic_ranges(
                cues,
                row,
                fallback_start=float(row.get("start_sec") or 0),
                fallback_end=row.get("end_sec"),
            )
            stitched = _stitch_complete_utterances(window) if window else []
            emerg = _emergency_hook_candidates(stitched or window, limit=4)
            if emerg:
                crafted = heuristic_craft_hooks(
                    emerg,
                    theme_title=f"{theme_title}: {row.get('text') or ''}".strip(": "),
                )
                for h in crafted[:1]:
                    h["topic_id"] = row.get("id")
                    h["topic_text"] = row.get("text")
                    topic_hooks.append(h)
                    hooks_out.append(h)
                    stats["backfilled"] += 1
        row["hooks"] = topic_hooks[:5]

        # Subtopics: keep only those with hooks; backfill from their window when possible.
        kept_subs: list[dict[str, Any]] = []
        for sub in row.get("subtopics") or []:
            s = dict(sub)
            sub_hooks = list(s.get("hooks") or [])
            if not sub_hooks:
                sw = _cues_for_topic_ranges(
                    cues,
                    {
                        "start_sec": s.get("start_sec"),
                        "end_sec": s.get("end_sec"),
                        "time_ranges": [
                            {
                                "start_sec": float(s.get("start_sec") or 0),
                                "end_sec": s.get("end_sec"),
                            }
                        ],
                    },
                    fallback_start=float(s.get("start_sec") or 0),
                    fallback_end=s.get("end_sec"),
                )
                emerg = _emergency_hook_candidates(
                    _stitch_complete_utterances(sw) if sw else sw, limit=3
                )
                if emerg:
                    crafted = heuristic_craft_hooks(
                        emerg,
                        theme_title=f"{theme_title}: {s.get('text') or ''}".strip(": "),
                    )
                    if crafted:
                        h = crafted[0]
                        h["topic_id"] = row.get("id")
                        h["topic_text"] = row.get("text")
                        h["subtopic_id"] = s.get("id")
                        h["subtopic_text"] = s.get("text")
                        sub_hooks = [h]
                        hooks_out.append(h)
                        stats["backfilled"] += 1
            if not sub_hooks:
                stats["pruned"] += 1
                continue
            s["hooks"] = sub_hooks[:3]
            kept_subs.append(s)
        row["subtopics"] = kept_subs

        if not row["hooks"] and not kept_subs:
            stats["pruned"] += 1
            continue
        kept.append(row)

    return kept, hooks_out, stats


def _dedupe_topic_tree_hooks(tree: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep the first unique hook placement across the complete topic tree."""
    seen_text: set[str] = set()
    seen_ids: set[str] = set()
    out: list[dict[str, Any]] = []
    for topic in tree or []:
        row = dict(topic)
        clean_subs: list[dict[str, Any]] = []
        # A hook explicitly mapped to a child gets first placement there.
        sections = list(row.get("subtopics") or []) + [row]
        for section in sections:
            kept: list[dict[str, Any]] = []
            for hook in list(section.get("hooks") or []):
                if not isinstance(hook, dict):
                    continue
                text_key = _normalize_hook_cmp(str(hook.get("text") or ""))
                id_key = str(hook.get("id") or "").strip()
                if (text_key and text_key in seen_text) or (id_key and id_key in seen_ids):
                    continue
                if text_key:
                    seen_text.add(text_key)
                if id_key:
                    seen_ids.add(id_key)
                kept.append(hook)
            if section is row:
                row["hooks"] = kept
            else:
                child = dict(section)
                child["hooks"] = kept
                if kept:
                    clean_subs.append(child)
        row["subtopics"] = clean_subs
        if row.get("hooks") or clean_subs:
            out.append(row)
    return out


def _hooks_from_topic_tree(tree: list[dict[str, Any]]) -> list[dict[str, Any]]:
    hooks: list[dict[str, Any]] = []
    seen_text: set[str] = set()
    seen_ids: set[str] = set()
    for topic in tree or []:
        for section in [topic, *(topic.get("subtopics") or [])]:
            for hook in section.get("hooks") or []:
                text_key = _normalize_hook_cmp(str(hook.get("text") or ""))
                id_key = str(hook.get("id") or "").strip()
                if (text_key and text_key in seen_text) or (id_key and id_key in seen_ids):
                    continue
                if text_key:
                    seen_text.add(text_key)
                if id_key:
                    seen_ids.add(id_key)
                hooks.append(hook)
    return hooks


def _hook_provenance_key(hook: dict[str, Any]) -> tuple[str, str, float, str]:
    spoken = str(hook.get("original_text") or hook.get("text") or "")
    return (
        str(hook.get("topic_id") or ""),
        str(hook.get("subtopic_id") or ""),
        round(float(hook.get("start_sec") or 0), 3),
        _normalize_hook_cmp(spoken),
    )


def _sync_topic_tree_hooks(
    tree: list[dict[str, Any]],
    hooks: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Replace tree hooks with their guarded display-copy versions."""
    by_source = {_hook_provenance_key(h): h for h in hooks}
    out: list[dict[str, Any]] = []
    for topic in tree:
        row = dict(topic)
        synced_hooks: list[dict[str, Any]] = []
        for hook in row.get("hooks") or []:
            provenance = _hook_provenance_key(hook)
            if provenance in by_source:
                synced_hooks.append(by_source[provenance])
        row["hooks"] = synced_hooks
        subs: list[dict[str, Any]] = []
        for sub in row.get("subtopics") or []:
            child = dict(sub)
            child_hooks: list[dict[str, Any]] = []
            for hook in child.get("hooks") or []:
                provenance = _hook_provenance_key(hook)
                if provenance in by_source:
                    child_hooks.append(by_source[provenance])
            child["hooks"] = child_hooks
            if child["hooks"]:
                subs.append(child)
        row["subtopics"] = subs
        if row["hooks"] or subs:
            out.append(row)
    return out


def _dedupe_hook_list(hooks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Deduplicate a flat hook list with the same shipped-tree keys."""
    out: list[dict[str, Any]] = []
    seen_text: set[str] = set()
    seen_ids: set[str] = set()
    for hook in hooks or []:
        if not isinstance(hook, dict):
            continue
        text_key = _normalize_hook_cmp(str(hook.get("text") or ""))
        id_key = str(hook.get("id") or "").strip()
        if (text_key and text_key in seen_text) or (id_key and id_key in seen_ids):
            continue
        if text_key:
            seen_text.add(text_key)
        if id_key:
            seen_ids.add(id_key)
        out.append(hook)
    return out


def _flatten_topic_tree(tree: list[dict[str, Any]]) -> list[dict[str, Any]]:
    flat: list[dict[str, Any]] = []
    for t in tree:
        flat.append(
            {
                "id": t.get("id"),
                "text": t.get("text"),
                "start_sec": t.get("start_sec"),
                "end_sec": t.get("end_sec"),
                "time_ranges": t.get("time_ranges") or [],
                "explanation": t.get("explanation"),
                "verbatim": False,
                "generated": True,
                "has_subtopics": bool(t.get("subtopics")),
            }
        )
        for sub in t.get("subtopics") or []:
            flat.append(
                {
                    "id": sub.get("id"),
                    "text": sub.get("text"),
                    "start_sec": sub.get("start_sec"),
                    "end_sec": sub.get("end_sec"),
                    "explanation": sub.get("explanation"),
                    "verbatim": False,
                    "generated": True,
                    "parent_topic_id": t.get("id"),
                    "is_subtopic": True,
                }
            )
    return flat


def _reindex_topic_tree(tree: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for i, t in enumerate(tree):
        row = dict(t)
        row["id"] = f"topic_{i + 1}"
        ranges = row.get("time_ranges") if isinstance(row.get("time_ranges"), list) else []
        if not ranges and row.get("start_sec") is not None:
            ranges = [
                {
                    "start_sec": float(row.get("start_sec") or 0),
                    "end_sec": float(row["end_sec"])
                    if row.get("end_sec") is not None
                    else float(row.get("start_sec") or 0),
                }
            ]
        row["time_ranges"] = ranges
        subs = []
        for j, sub in enumerate(row.get("subtopics") or []):
            s = dict(sub)
            s["id"] = f"topic_{i + 1}_sub_{j + 1}"
            s.setdefault("hooks", [])
            subs.append(s)
        row["subtopics"] = subs
        row.setdefault("hooks", [])
        out.append(row)
    return out


def _best_subtopic_for_hook(
    hook: dict[str, Any],
    subtopics: list[dict[str, Any]],
) -> dict[str, Any] | None:
    hs = float(hook.get("start_sec") or 0)
    best: dict[str, Any] | None = None
    best_dist = 1e18
    for sub in subtopics:
        ss = float(sub.get("start_sec") or 0)
        se = sub.get("end_sec")
        if se is not None and ss <= hs <= float(se) + 0.5:
            return sub
        dist = abs(hs - ss)
        if dist < best_dist:
            best_dist = dist
            best = sub
    return best if best_dist < 45 else None


async def _llm_hooks_for_singular_topic(
    *,
    hooks: list[dict[str, Any]],
    topic_title: str,
    topic_explanation: str,
    theme_title: str,
    theme_summary: str,
    used_angles: list[str],
    api_key: str | None = None,
    model: str = "",
    claude_api_key: str | None = None,
    claude_model: str = "",
    provider: str = "auto",
    openrouter_api_key: str | None = None,
    openrouter_model: str = "",
    openrouter_base_url: str = "",
    max_hooks: int = 2,
) -> list[dict[str, Any]]:
    """Craft Instagram hooks for ONE topic only — never reuse prior topics' angles."""
    payload = []
    for i, h in enumerate(hooks):
        payload.append(
            {
                "i": i,
                "spoken": str(h.get("text") or "").strip()[:400],
                "start_sec": h.get("start_sec"),
                "end_sec": h.get("end_sec"),
            }
        )
    used = [u for u in used_angles if (u or "").strip()][:24]
    want = max(1, min(4, int(max_hooks or 2)))
    prompt = (
        "You are an expert Instagram copywriter writing reel/carousel hooks.\n"
        "Generate punchy HOOK lines for ONE singular topic only.\n"
        f"{_HOOK_CRAFT_BRIEF}"
        "CRAFT RULES:\n"
        f"- This batch is ONLY about the topic: “{topic_title}”.\n"
        f"- Topic context: {topic_explanation or '(from transcript window)'}\n"
        "- Do NOT invent hooks about other topics. Do NOT reuse or paraphrase any "
        "already-used hook angles listed below.\n"
        "- Derive each hook FROM the spoken window — NEVER paste or lightly trim the transcript.\n"
        "- FORBIDDEN: returning the spoken line unchanged, a substring of it, or a near-copy "
        "that only drops filler words. You MUST rewrite into a fresh Instagram hook.\n"
        "- 6–14 words; one idea; natural English; keep the true claim of what was said, "
        "but change the wording.\n"
        f"- Prefer returning up to {want} strongest hooks (you may skip weak indices).\n"
        f"Parent theme: {theme_title}\n"
        f"Theme summary: {theme_summary}\n"
        f"Already-used hook angles (FORBIDDEN to overlap): {json.dumps(used, ensure_ascii=False)}\n"
        'Return ONLY JSON array: {"i": number, "hook": "crafted English hook"}.\n\n'
        f"Spoken windows:\n{json.dumps(payload, ensure_ascii=False)}"
    )
    text, _provider = await _llm_complete_json(
        prompt=prompt,
        temperature=0.45,
        max_tokens=1800,
        api_key=api_key,
        model=model,
        claude_api_key=claude_api_key,
        claude_model=claude_model,
        provider=provider,
        openrouter_api_key=openrouter_api_key,
        openrouter_model=openrouter_model,
        openrouter_base_url=openrouter_base_url,
        json_root="array",
    )
    raw = _loads_json_array(text)
    if not raw:
        return []
    by_i: dict[int, str] = {}
    for row in raw:
        if not isinstance(row, dict):
            continue
        try:
            i = int(row.get("i"))
        except (TypeError, ValueError):
            continue
        hook = str(row.get("hook") or "").strip()
        if hook:
            by_i[i] = hook[:200]

    out: list[dict[str, Any]] = []
    used_norm = {" ".join(u.lower().split()) for u in used}
    for i, h in enumerate(hooks):
        crafted = by_i.get(i)
        if not crafted:
            continue
        norm = " ".join(crafted.lower().split())
        if norm in used_norm:
            continue
        if any(_jaccard(_token_set(crafted), _token_set(u)) >= 0.55 for u in used):
            continue
        spoken = str(h.get("text") or "")
        if _nearly_verbatim(crafted, spoken):
            continue
        if not 5 <= len(crafted.split()) <= 18:
            continue
        if not _hook_numbers_are_grounded(crafted, spoken):
            continue
        if not _hook_is_readable(crafted):
            continue
        row = dict(h)
        row["original_text"] = spoken
        row["text"] = crafted
        row["verbatim"] = False
        row["analysed"] = True
        row["topic_id"] = h.get("topic_id")
        row["topic_text"] = topic_title
        out.append(row)
        used_norm.add(norm)
        if len(out) >= want:
            break
    return out


async def _llm_hooks_for_combined_topics(
    *,
    hooks: list[dict[str, Any]],
    topics: list[dict[str, Any]],
    theme_title: str,
    theme_summary: str,
    api_key: str | None = None,
    model: str = "",
    claude_api_key: str | None = None,
    claude_model: str = "",
    provider: str = "auto",
    openrouter_api_key: str | None = None,
    openrouter_model: str = "",
    openrouter_base_url: str = "",
    max_hooks: int = 4,
) -> list[dict[str, Any]]:
    """Craft Instagram hooks that synthesize ALL selected topics together."""
    payload = []
    for i, h in enumerate(hooks):
        payload.append(
            {
                "i": i,
                "spoken": str(h.get("text") or "").strip()[:400],
                "start_sec": h.get("start_sec"),
                "end_sec": h.get("end_sec"),
                "topic": str(h.get("topic_text") or "")[:160],
            }
        )
    topic_rows = [
        {
            "title": str(t.get("text") or "").strip()[:200],
            "explanation": str(t.get("explanation") or "")[:280],
        }
        for t in topics
        if str(t.get("text") or "").strip()
    ]
    want = max(2, min(4, int(max_hooks or 4)))
    titles = [r["title"] for r in topic_rows]
    prompt = (
        "You are an expert Instagram copywriter writing reel/carousel hooks.\n"
        "Generate punchy HOOK lines for a COMBINED set of selected topics — "
        "NOT one hook per topic, and NOT separate batches.\n"
        f"{_HOOK_CRAFT_BRIEF}"
        "CRAFT RULES:\n"
        f"- Treat these topics as ONE story angle together: {json.dumps(titles, ensure_ascii=False)}\n"
        f"- Topic context: {json.dumps(topic_rows, ensure_ascii=False)}\n"
        "- Return 2–4 TOTAL hooks for the whole selection (hard cap).\n"
        "- Prefer hooks that bridge or synthesize multiple selected topics when possible; "
        "a single-topic hook is OK only if it still serves the combined story.\n"
        "- Do NOT invent a separate hook for every topic. Do NOT pad with weak near-duplicates.\n"
        "- Derive each hook FROM the spoken windows — NEVER paste or lightly trim the transcript.\n"
        "- FORBIDDEN: returning the spoken line unchanged, a substring of it, or a near-copy "
        "that only drops filler words. You MUST rewrite into a fresh Instagram hook.\n"
        "- 6–14 words; one idea; natural English; keep the true claim of what was said, "
        "but change the wording.\n"
        f"- Prefer returning up to {want} strongest hooks (you may skip weak indices).\n"
        f"Parent theme: {theme_title}\n"
        f"Theme summary: {theme_summary}\n"
        'Return ONLY JSON array: {"i": number, "hook": "crafted English hook"}.\n\n'
        f"Spoken windows:\n{json.dumps(payload, ensure_ascii=False)}"
    )
    text, _provider = await _llm_complete_json(
        prompt=prompt,
        temperature=0.45,
        max_tokens=1800,
        api_key=api_key,
        model=model,
        claude_api_key=claude_api_key,
        claude_model=claude_model,
        provider=provider,
        openrouter_api_key=openrouter_api_key,
        openrouter_model=openrouter_model,
        openrouter_base_url=openrouter_base_url,
        json_root="array",
    )
    raw = _loads_json_array(text)
    if not raw:
        return []
    by_i: dict[int, str] = {}
    for row in raw:
        if not isinstance(row, dict):
            continue
        try:
            i = int(row.get("i"))
        except (TypeError, ValueError):
            continue
        hook = str(row.get("hook") or "").strip()
        if hook:
            by_i[i] = hook[:200]

    out: list[dict[str, Any]] = []
    used_norm: set[str] = set()
    for i, h in enumerate(hooks):
        crafted = by_i.get(i)
        if not crafted:
            continue
        norm = " ".join(crafted.lower().split())
        if norm in used_norm:
            continue
        if any(_jaccard(_token_set(crafted), _token_set(u)) >= 0.55 for u in used_norm):
            continue
        spoken = str(h.get("text") or "")
        if _nearly_verbatim(crafted, spoken):
            continue
        if not 5 <= len(crafted.split()) <= 18:
            continue
        if not _hook_numbers_are_grounded(crafted, spoken):
            continue
        if not _hook_is_readable(crafted):
            continue
        row = dict(h)
        row["original_text"] = spoken
        row["text"] = crafted
        row["verbatim"] = False
        row["analysed"] = True
        row["combined_topics"] = True
        out.append(row)
        used_norm.add(norm)
        if len(out) >= want:
            break
    return out


async def craft_hooks_for_selected_topics_async(
    cues: list[tuple[float, float | None, str]],
    *,
    selected_topics: list[dict[str, Any]],
    theme_title: str = "",
    theme_summary: str = "",
    min_hooks: int = 2,
    max_hooks: int = 4,
    api_key: str | None = None,
    model: str = "",
    claude_api_key: str | None = None,
    claude_model: str = "",
    provider: str = "auto",
    openrouter_api_key: str | None = None,
    openrouter_model: str = "",
    openrouter_base_url: str = "",
    english_cues: list[tuple[float, float | None, str]] | None = None,
) -> dict[str, Any]:
    """Craft 2–4 total hooks for the COMBINED selected topics (not per-topic)."""
    topics = [t for t in selected_topics if isinstance(t, dict) and str(t.get("text") or "").strip()]
    if not topics:
        return {"hooks": [], "topics": [], "topic_tree": [], "source": "empty"}

    min_n = max(2, int(min_hooks or 2))
    max_n = max(min_n, min(4, int(max_hooks or 4)))
    primary = prefer_english_cues(english_cues or cues)
    pool = primary if primary else cues
    cue_corpus = [str(t or "") for _s, _e, t in pool if (t or "").strip()]

    # One shared candidate pool across every selected topic window.
    combined_window: list[tuple[float, float | None, str]] = []
    seen_cue: set[tuple[float, str]] = set()
    candidates: list[dict[str, Any]] = []
    for topic in topics:
        title = str(topic.get("text") or "").strip()
        topic_window = _cues_for_topic_ranges(
            pool,
            topic,
            fallback_start=float(topic.get("start_sec") or 0),
            fallback_end=topic.get("end_sec"),
        )
        if not topic_window:
            topic_window = _cues_in_range(
                pool,
                float(topic.get("start_sec") or 0),
                topic.get("end_sec"),
            )
        for cue in topic_window:
            key = (round(float(cue[0] or 0), 3), str(cue[2] or "").strip()[:80])
            if key in seen_cue:
                continue
            seen_cue.add(key)
            combined_window.append(cue)
        stitched = _stitch_complete_utterances(topic_window)
        topic_cands = _pick_contextual_hooks(stitched)
        if not topic_cands:
            topic_cands = _emergency_hook_candidates(stitched or topic_window, limit=4)
        for h in topic_cands:
            row = dict(h)
            row["topic_id"] = topic.get("id")
            row["topic_text"] = title
            candidates.append(row)

    # Prefer candidates that touch more of the combined selection / business punch.
    topic_titles = [str(t.get("text") or "") for t in topics]
    candidates = sorted(
        candidates,
        key=lambda h: -(
            max((_topic_text_overlap(title, str(h.get("text") or "")) for title in topic_titles), default=0.0)
            + 0.35 * _business_hook_score(str(h.get("text") or ""))
        ),
    )
    # Cap pool so the LLM sees a tight combined window.
    candidates = _dedupe_hook_list(candidates)[:12]
    if not candidates and combined_window:
        candidates = _emergency_hook_candidates(
            _stitch_complete_utterances(combined_window) or combined_window,
            limit=8,
        )
        for h in candidates:
            h.setdefault("topic_id", topics[0].get("id"))
            h.setdefault("topic_text", topics[0].get("text"))

    all_hooks: list[dict[str, Any]] = []
    if candidates and _llm_has_any_key(
        api_key=api_key,
        claude_api_key=claude_api_key,
        openrouter_api_key=openrouter_api_key,
        openrouter_model=openrouter_model,
    ):
        try:
            all_hooks = await _llm_hooks_for_combined_topics(
                hooks=candidates,
                topics=topics,
                theme_title=theme_title,
                theme_summary=theme_summary,
                api_key=api_key,
                model=model,
                claude_api_key=claude_api_key,
                claude_model=claude_model,
                provider=provider,
                openrouter_api_key=openrouter_api_key,
                openrouter_model=openrouter_model,
                openrouter_base_url=openrouter_base_url,
                max_hooks=max_n,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("combined-topic hook craft failed: %s", exc)

    if not all_hooks and candidates:
        combined_label = " + ".join(
            str(t.get("text") or "").strip() for t in topics if str(t.get("text") or "").strip()
        )[:180]
        all_hooks = heuristic_craft_hooks(
            candidates,
            theme_title=f"{theme_title}: {combined_label}".strip(": "),
        )[:max_n]
        for h in all_hooks:
            h["combined_topics"] = True

    all_hooks = _dedupe_hook_list(all_hooks)
    all_hooks, _guard = enforce_non_verbatim_hooks(
        all_hooks,
        cue_corpus,
        theme_title=theme_title,
    )
    all_hooks = all_hooks[:max_n]

    if len(all_hooks) < min_n and candidates:
        extras = heuristic_craft_hooks(candidates, theme_title=theme_title)
        have = {" ".join(str(h.get("text") or "").lower().split()) for h in all_hooks}
        for h in extras:
            key = " ".join(str(h.get("text") or "").lower().split())
            if not key or key in have:
                continue
            row = dict(h)
            row["combined_topics"] = True
            all_hooks.append(row)
            have.add(key)
            if len(all_hooks) >= min_n:
                break
        all_hooks = all_hooks[:max_n]

    all_hooks.sort(key=lambda r: float(r.get("start_sec") or 0))
    for i, h in enumerate(all_hooks):
        h["id"] = f"hook_{i + 1}"
        h["combined_topics"] = True
        # Clear per-topic nesting so the UI shows one shared list.
        h.pop("topic_id", None)
        h["topic_text"] = " + ".join(
            str(t.get("text") or "").strip() for t in topics if str(t.get("text") or "").strip()
        )[:240]

    # Keep selected topics in the tree WITHOUT nested hooks — hooks live flat.
    topic_tree: list[dict[str, Any]] = []
    for topic in topics:
        node = dict(topic)
        node["hooks"] = []
        node.setdefault("subtopics", [])
        topic_tree.append(node)

    return {
        "hooks": all_hooks,
        "topics": _flatten_topic_tree(topic_tree)[:_MAX_TOPICS],
        "topic_tree": _reindex_topic_tree(topic_tree)[:_MAX_TOPICS],
        "source": "selected_topics_combined",
        "hook_count": len(all_hooks),
        "min_hooks": min_n,
        "max_hooks": max_n,
        "combined_topic_count": len(topics),
    }


async def _llm_topics_from_theme(
    *,
    theme_title: str,
    theme_summary: str,
    transcript: str,
    search_entity: str | None,
    api_key: str | None = None,
    model: str = "",
    claude_api_key: str | None = None,
    claude_model: str = "",
    provider: str = "auto",
    openrouter_api_key: str | None = None,
    openrouter_model: str = "",
    openrouter_base_url: str = "",
    theme_start: float = 0.0,
    theme_end: float | None = None,
    hooks: list[dict[str, Any]] | None = None,
    stitched: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    entity = (search_entity or "").strip()
    prompt = (
        "You carefully READ the timed transcript and infer COHESIVE TOPICS — real thematic "
        "clusters where the speaker takes a direction — for one selected theme.\n"
        "Topics are thematic chapter titles grounded in what was said — NOT transcript quotes, "
        "NOT vague umbrellas that ignore most of the talk.\n"
        "Each topic will seed Instagram reel/carousel hooks, so choose the angles a viewer "
        "would stop for: the startling number, the stake, the contradiction, the "
        "counterintuitive move — not the dull category label.\n"
        "Prioritize concrete business and entrepreneurship insights: strategies, mechanisms, "
        "mistakes, contrarian views, playbooks, numbers, trade-offs, and outcomes involving "
        "customers, distribution, product, pricing, sales, growth, capital, profit, operations, "
        "teams, markets, wealth creation, or policy effects on enterprise.\n"
        "Rules:\n"
        "- Aim for 5–10 topics when the transcript supports it (order = chronology)\n"
        "- Each topic: 2–8 words; ONE concrete idea; natural English; insight-led rather than "
        "a generic category when the transcript supports it\n"
        "- Intriguing but honest: name the subject truthfully, never a bait title the "
        "transcript cannot deliver on\n"
        "- Never boring or cliched: ban filler labels like 'Introduction', 'Key Takeaways', "
        "'Business Insights', 'The Journey'\n"
        "- Cluster by meaning/direction pivots in the transcript\n"
        "- No incomplete thoughts; no near-duplicates "
        "(e.g. 'Student-First Philosophy' ≈ 'Student-Centric Decisions' — keep one)\n"
        "- Keep distinct adjacent angles; do not collapse the talk into 2–3 generic labels\n"
        "- Use creative editorial phrasing, but never invent a claim, number, or lesson; "
        "never explicit or demeaning\n"
        f"Theme title: {theme_title}\n"
        f"Theme summary: {theme_summary}\n"
        f"Search entity: {entity or '(none)'}\n"
        "Return ONLY a JSON array of strings.\n\n"
        f"Theme transcript (READ IT):\n{transcript[:_TOPIC_CHUNK_CHARS]}"
    )
    text, _provider = await _llm_complete_json(
        prompt=prompt,
        temperature=0.4,
        max_tokens=1800,
        api_key=api_key,
        model=model,
        claude_api_key=claude_api_key,
        claude_model=claude_model,
        provider=provider,
        openrouter_api_key=openrouter_api_key,
        openrouter_model=openrouter_model,
        openrouter_base_url=openrouter_base_url,
    )
    raw = _loads_json_array(text)
    if not raw:
        return []
    labels: list[str] = []
    seen: set[str] = set()
    for item in raw:
        label = str(
            item if not isinstance(item, dict) else item.get("text") or item.get("topic") or ""
        ).strip()
        if not label or len(label.split()) > 10:
            continue
        key = label.lower()
        if key in seen:
            continue
        seen.add(key)
        labels.append(label[:120])
        if len(labels) >= _MAX_TOPICS:
            break

    spans = _spread_topic_spans(
        len(labels),
        theme_start=theme_start,
        theme_end=theme_end,
        hooks=hooks,
        stitched=stitched,
    )
    out: list[dict[str, Any]] = []
    for i, label in enumerate(labels):
        s, e = spans[i] if i < len(spans) else (float(theme_start or 0), None)
        out.append(
            {
                "id": f"topic_{i + 1}",
                "text": label,
                "start_sec": float(s),
                "end_sec": float(e) if e is not None else None,
                "verbatim": False,
                "generated": True,
            }
        )
    return out


def _topic_angle_from_hook(hook: str) -> str:
    words = " ".join((hook or "").split()).split()
    if len(words) < 4:
        return ""
    # Take a noun-ish slice without dumping the whole quote
    slice_words = words[0:6] if words[0][0:1].isupper() else words[:5]
    label = " ".join(slice_words).rstrip(".,;:!?")
    if len(label.split()) < 2:
        return ""
    return label[:80]


def _trim_to_clause(text: str, max_words: int = 28) -> str:
    words = text.split()
    if len(words) <= max_words:
        return text
    cut = " ".join(words[:max_words])
    # Prefer ending at punctuation inside the cut
    m = list(re.finditer(r"[.!?]", cut))
    if m:
        return cut[: m[-1].end()].strip()
    return cut.rstrip(",;:") + "…"


def _cues_in_range(
    cues: list[tuple[float, float | None, str]],
    start_sec: float,
    end_sec: float | None,
) -> list[tuple[float, float | None, str]]:
    out: list[tuple[float, float | None, str]] = []
    for s, e, t in cues:
        if not (t or "").strip():
            continue
        if s < start_sec - 0.05:
            continue
        if end_sec is not None and s > float(end_sec) + 0.25:
            continue
        out.append((s, e, t))
    return out


async def deduce_directional_intent(
    *,
    theme_title: str,
    theme_summary: str,
    hooks: list[str],
    topics: list[str],
    search_entity: str | None,
    api_key: str | None,
    model: str,
    claude_api_key: str | None = None,
    claude_model: str = "",
    provider: str = "auto",
    openrouter_api_key: str | None = None,
    openrouter_model: str = "",
    openrouter_base_url: str = "",
) -> dict[str, Any]:
    """Intent discovery only — does not write a script."""
    entity = (search_entity or "").strip()
    fallback_label = _fallback_intent(theme_title, hooks, topics, entity)
    if not _llm_has_any_key(
        api_key=api_key,
        claude_api_key=claude_api_key,
        openrouter_api_key=openrouter_api_key,
        openrouter_model=openrouter_model,
    ):
        return {"intent": fallback_label, "intent_score": 0.55, "source": "fallback"}

    try:
        prompt = (
            "Deduce the creator's directional intent for a video carousel segment. "
            "Do NOT write a script. Return ONLY JSON: "
            '{"intent": "one sentence", "intent_score": 0.0-1.0}\n'
            f"Theme: {theme_title}\nSummary: {theme_summary}\n"
            f"Entity: {entity or '(none)'}\n"
            f"Hooks (verbatim): {hooks}\nTopics (verbatim): {topics}\n"
        )
        raw, used = await _llm_complete_json(
            prompt=prompt,
            system=(
                "You deduce carousel intent from themes and transcript hooks. "
                "Return ONLY valid JSON."
            ),
            temperature=0.2,
            max_tokens=400,
            api_key=api_key,
            model=model,
            claude_api_key=claude_api_key,
            claude_model=claude_model,
            provider=provider,
            openrouter_api_key=openrouter_api_key,
            openrouter_model=openrouter_model,
            openrouter_base_url=openrouter_base_url,
        )
        text = (raw or "").strip()
        m = re.search(r"\{[\s\S]*\}", text)
        parsed = json.loads(m.group() if m else text or "{}")
        if not isinstance(parsed, dict):
            parsed = {}
        intent = str(parsed.get("intent") or fallback_label).strip()[:400]
        score = parsed.get("intent_score", 0.7)
        try:
            score_f = max(0.0, min(1.0, float(score)))
        except (TypeError, ValueError):
            score_f = 0.7
        return {"intent": intent, "intent_score": score_f, "source": used or "llm"}
    except Exception as exc:  # noqa: BLE001
        logger.warning("intent deduction failed: %s", exc)
        return {"intent": fallback_label, "intent_score": 0.5, "source": "fallback"}


def _fallback_intent(title: str, hooks: list[str], topics: list[str], entity: str) -> str:
    bits = [f"Tune into “{title}”"]
    if entity:
        bits.append(f"centered on {entity}")
    if hooks:
        bits.append(f"opening on “{hooks[0][:80]}”")
    if topics:
        bits.append(f"developing “{topics[0][:80]}”")
    return " — ".join(bits)


def _complete_theme_title(title: str, *, max_words: int = 12) -> str:
    cleaned = " ".join((title or "").split()).strip()
    if not cleaned:
        return ""
    words = cleaned.split()
    dangling = {
        "to", "be", "in", "on", "at", "of", "for", "and", "or", "the", "a", "an",
        "with", "from", "as", "is", "are", "was", "were", "their", "our", "my",
    }
    while words and words[-1].lower().strip(".,;:!?") in dangling:
        words.pop()
    if len(words) > max_words:
        words = words[:max_words]
        while words and words[-1].lower().strip(".,;:!?") in dangling:
            words.pop()
    return " ".join(words) if words else cleaned[:80]


def _as_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _token_set(text: str) -> set[str]:
    stop = {
        "the", "a", "an", "and", "or", "to", "of", "in", "on", "for", "with", "is", "are",
        "that", "this", "it", "as", "at", "by", "from", "be", "you", "your", "our", "we",
        "first", "centric", "based", "driven", "oriented", "focused", "approach", "philosophy",
        "decisions", "decision", "mindset", "way", "ways",
    }
    out: set[str] = set()
    for raw in re.findall(r"[a-z0-9']+", (text or "").lower()):
        if raw in stop or len(raw) <= 2:
            continue
        # Light stemming so "students" ≈ "student".
        token = raw[:-1] if len(raw) > 4 and raw.endswith("s") and not raw.endswith("ss") else raw
        out.add(token)
    return out


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def heuristic_topic_dedupe(
    topics: list[dict[str, Any]],
    *,
    threshold: float = 0.45,
) -> list[dict[str, Any]]:
    """Drop near-duplicate topic labels by token overlap (e.g. student-first ≈ student-centric)."""
    kept: list[dict[str, Any]] = []
    kept_tokens: list[set[str]] = []
    for row in topics:
        if not isinstance(row, dict):
            continue
        text = str(row.get("text") or "").strip()
        if not text:
            continue
        toks = _token_set(text)
        if any(_jaccard(toks, existing) >= threshold for existing in kept_tokens):
            continue
        kept.append(dict(row))
        kept_tokens.append(toks)
    for i, t in enumerate(kept):
        t["id"] = f"topic_{i + 1}"
    return kept


# Back-compat private alias
_heuristic_topic_dedupe = heuristic_topic_dedupe


def _merge_topic_nodes(
    topics: list[dict[str, Any]],
    indices: list[int],
    *,
    label: str,
) -> dict[str, Any]:
    """Union provenance from merged topic indices onto one carousel-ready node."""
    members: list[dict[str, Any]] = []
    for idx in indices:
        if 0 <= idx < len(topics) and isinstance(topics[idx], dict):
            members.append(topics[idx])
    src = members[0] if members else (topics[0] if topics else {})
    item = dict(src)
    item["text"] = label[:120]
    starts: list[float] = []
    ends: list[float] = []
    ranges: list[dict[str, Any]] = []
    subtopics: list[dict[str, Any]] = []
    hooks: list[dict[str, Any]] = []
    explanations: list[str] = []
    for cand in members or [item]:
        try:
            starts.append(float(cand.get("start_sec") or 0))
        except (TypeError, ValueError):
            pass
        try:
            if cand.get("end_sec") is not None:
                ends.append(float(cand.get("end_sec")))
        except (TypeError, ValueError):
            pass
        for rng in cand.get("time_ranges") or []:
            if isinstance(rng, dict):
                ranges.append(dict(rng))
        for sub in cand.get("subtopics") or []:
            if isinstance(sub, dict):
                subtopics.append(dict(sub))
        for hook in cand.get("hooks") or []:
            if isinstance(hook, dict):
                hooks.append(dict(hook))
        note = str(cand.get("explanation") or "").strip()
        if note:
            explanations.append(note)
    if starts:
        item["start_sec"] = min(starts)
    if ends:
        item["end_sec"] = max(ends)
    if ranges:
        item["time_ranges"] = ranges
    if subtopics:
        item["subtopics"] = subtopics
    if hooks:
        item["hooks"] = hooks
    if explanations and not str(item.get("explanation") or "").strip():
        item["explanation"] = explanations[0][:300]
    item["verbatim"] = False
    item["generated"] = True
    item["semantic_merge"] = True
    return item


async def dedupe_topics_semantic(
    topics: list[dict[str, Any]],
    *,
    theme_title: str = "",
    api_key: str | None = None,
    model: str = "",
    claude_api_key: str | None = None,
    claude_model: str = "",
    provider: str = "auto",
    openrouter_api_key: str | None = None,
    openrouter_model: str = "",
    openrouter_base_url: str = "",
) -> list[dict[str, Any]]:
    """Merge semantically duplicate topics via the studio LLM; fall back to overlap."""
    if len(topics) < 2:
        return topics

    heuristic = _heuristic_topic_dedupe(topics)
    if not _llm_has_any_key(
        api_key=api_key,
        claude_api_key=claude_api_key,
        openrouter_api_key=openrouter_api_key,
        openrouter_model=openrouter_model,
    ):
        return heuristic

    payload = [
        {
            "i": i,
            "text": str(t.get("text") or "").strip(),
            "start_sec": t.get("start_sec"),
            "end_sec": t.get("end_sec"),
        }
        for i, t in enumerate(topics)
        if str(t.get("text") or "").strip()
    ]
    if len(payload) < 2:
        return topics

    prompt = (
        "Merge duplicate / near-duplicate TOPIC LABELS into a unique cohesive set.\n"
        "Examples of duplicates to merge: 'Student-First Philosophy' + "
        "'Student-Centric Decisions' → keep one best label.\n"
        "Rules:\n"
        "- Keep only UNIQUE ideas (semantic similarity / content overlap = merge)\n"
        "- Prefer the clearest, most carousel-ready label when merging\n"
        "- Preserve a coherent narrative set for the theme — drop redundant or scattered labels\n"
        "- Do not collapse a rich talk into fewer than half of the input topics\n"
        "- Return topics in chronological preference (use earliest start_sec of the merge group)\n"
        f"Theme: {theme_title or '(none)'}\n"
        "Return ONLY a JSON array of objects: "
        '{"text": "label", "from_indices": [i, ...]}.\n\n'
        f"Topics:\n{json.dumps(payload, ensure_ascii=False)}"
    )
    try:
        text, _provider = await _llm_complete_json(
            prompt=prompt,
            system=(
                "You merge duplicate carousel topic labels. Return ONLY a JSON array."
            ),
            temperature=0.2,
            max_tokens=1800,
            api_key=api_key,
            model=model,
            claude_api_key=claude_api_key,
            claude_model=claude_model,
            provider=provider,
            openrouter_api_key=openrouter_api_key,
            openrouter_model=openrouter_model,
            openrouter_base_url=openrouter_base_url,
            json_root="array",
        )
        raw = _loads_json_array(text)
    except Exception as exc:  # noqa: BLE001
        logger.warning("LLM topic dedupe failed: %s", exc)
        return heuristic

    if not isinstance(raw, list) or not raw:
        return heuristic

    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in raw:
        if not isinstance(row, dict):
            continue
        label = str(row.get("text") or "").strip()
        if not label:
            continue
        key = label.lower()
        if key in seen:
            continue
        seen.add(key)
        raw_indices = row.get("from_indices") or row.get("indices") or []
        indices: list[int] = []
        if isinstance(raw_indices, list):
            for idx in raw_indices:
                try:
                    i = int(idx)
                except (TypeError, ValueError):
                    continue
                if 0 <= i < len(topics) and i not in indices:
                    indices.append(i)
        if not indices:
            match = next(
                (
                    i
                    for i, t in enumerate(topics)
                    if str(t.get("text") or "").strip().lower() == key
                ),
                0,
            )
            indices = [match]
        item = _merge_topic_nodes(topics, indices, label=label)
        out.append(item)
        if len(out) >= _MAX_TOPICS:
            break

    if not out:
        return heuristic
    min_keep = max(2, (len(heuristic) + 1) // 2)
    if len(out) < min_keep and len(heuristic) >= min_keep:
        logger.info(
            "carousel topic semantic dedupe rejected collapse %d → %d (min_keep=%d)",
            len(heuristic),
            len(out),
            min_keep,
        )
        return heuristic
    for i, t in enumerate(out):
        t["id"] = f"topic_{i + 1}"
    out.sort(key=lambda row: float(row.get("start_sec") or 0))
    # Second pass: heuristic on LLM output to catch leftover near-dupes.
    return _heuristic_topic_dedupe(out)


def cue_preview_lines(
    cues: list[tuple[float, float | None, str]],
    *,
    start_sec: float,
    end_sec: float | None,
    limit: int = 12,
) -> list[dict[str, Any]]:
    rows = []
    for s, e, t in _cues_in_range(cues, start_sec, end_sec)[:limit]:
        rows.append(
            {
                "start_sec": float(s),
                "end_sec": float(e) if e is not None else None,
                "text": " ".join((t or "").split()),
                "label": format_cue_line(s, e, t or ""),
            }
        )
    return rows


def merge_theme_extracts(
    extracts: list[dict[str, Any]],
    *,
    max_hooks: int = _MAX_MERGED_HOOKS,
    max_topics: int = _MAX_MERGED_TOPICS,
) -> dict[str, Any]:
    """Merge per-theme extracts: unique hooks/topics sorted by start time."""
    hooks: list[dict[str, Any]] = []
    topics: list[dict[str, Any]] = []
    seen_hooks: set[str] = set()
    seen_topics: set[str] = set()
    any_translated = False
    english_source: str | None = None

    for payload in extracts:
        if not isinstance(payload, dict):
            continue
        if payload.get("any_translated"):
            any_translated = True
        if english_source is None and payload.get("english_source"):
            english_source = str(payload.get("english_source"))
        for row in payload.get("hooks") or []:
            if not isinstance(row, dict):
                continue
            text = str(row.get("text") or "").strip()
            if not text:
                continue
            key = text.lower()
            if key in seen_hooks:
                continue
            seen_hooks.add(key)
            item = dict(row)
            item["text"] = text
            hooks.append(item)
            if item.get("translated"):
                any_translated = True
        for row in payload.get("topics") or []:
            if not isinstance(row, dict):
                continue
            text = str(row.get("text") or "").strip()
            if not text:
                continue
            key = text.lower()
            if key in seen_topics:
                continue
            seen_topics.add(key)
            item = dict(row)
            item["text"] = text
            topics.append(item)
            if item.get("translated"):
                any_translated = True

    hooks.sort(key=lambda r: (float(r.get("start_sec") or 0), str(r.get("text") or "")))
    topics.sort(key=lambda r: (float(r.get("start_sec") or 0), str(r.get("text") or "")))
    hooks = hooks[: max(1, int(max_hooks))]
    topics = _heuristic_topic_dedupe(topics)[: max(1, int(max_topics))]
    for i, h in enumerate(hooks):
        h["id"] = f"hook_{i + 1}"
    for i, t in enumerate(topics):
        t["id"] = f"topic_{i + 1}"

    return {
        "hooks": hooks,
        "topics": topics,
        "any_translated": any_translated,
        "english_source": english_source,
        "hooks_english": (
            all(is_english_text(str(h.get("text") or "")) for h in hooks) if hooks else True
        ),
        "topics_english": (
            all(is_english_text(str(t.get("text") or "")) for t in topics) if topics else True
        ),
    }


def merge_preview_windows(
    cues: list[tuple[float, float | None, str]],
    windows: list[tuple[float, float | None]],
    *,
    limit: int = 24,
) -> list[dict[str, Any]]:
    """Union of cue preview lines across theme windows, time-ordered unique."""
    seen: set[tuple[float, str]] = set()
    rows: list[dict[str, Any]] = []
    for start, end in windows:
        for row in cue_preview_lines(cues, start_sec=float(start or 0), end_sec=end, limit=limit):
            key = (round(float(row["start_sec"]), 2), str(row.get("text") or "")[:80].lower())
            if key in seen:
                continue
            seen.add(key)
            rows.append(row)
    rows.sort(key=lambda r: float(r.get("start_sec") or 0))
    return rows[:limit]


def _normalize_highlight_indices(text: str, raw_indices: Any, raw_words: Any) -> tuple[list[int], list[str]]:
    """Return validated 0-based word indices + matching words for yellow highlights."""
    words = [w for w in re.split(r"\s+", (text or "").strip()) if w]
    n = len(words)
    indices: list[int] = []
    if isinstance(raw_indices, list):
        for v in raw_indices:
            try:
                i = int(v)
            except (TypeError, ValueError):
                continue
            if 0 <= i < n and i not in indices:
                indices.append(i)
    if not indices and isinstance(raw_words, list):
        lowered = [w.lower().strip(".,!?;:\"'()[]") for w in words]
        for rw in raw_words:
            token = str(rw or "").lower().strip(".,!?;:\"'()[]")
            if not token:
                continue
            for i, w in enumerate(lowered):
                if w == token and i not in indices:
                    indices.append(i)
                    break
    # MU style: highlight 1–3 punchy words, never the whole line.
    if len(indices) > 3:
        indices = indices[:3]
    if not indices and n >= 2:
        # Heuristic fallback: emphasize a mid content word (skip tiny function words).
        stop = {
            "a", "an", "the", "and", "or", "to", "of", "in", "on", "for", "with",
            "is", "are", "was", "were", "be", "it", "this", "that", "you", "your",
            "we", "our", "i", "my", "from", "as", "at", "by", "not", "but",
        }
        for i, w in enumerate(words):
            core = w.lower().strip(".,!?;:\"'()[]")
            if len(core) >= 4 and core not in stop:
                indices = [i]
                break
    highlight_words = [words[i] for i in indices if 0 <= i < n]
    return indices, highlight_words


def _heuristic_highlight_for_line(text: str) -> tuple[str, list[int], list[str]]:
    cleaned = " ".join((text or "").split()).strip()
    if not cleaned:
        return "", [], []
    indices, words = _normalize_highlight_indices(cleaned, None, None)
    return cleaned[:220], indices, words


async def polish_slides_instagram_copy(
    slides: list[dict[str, Any]],
    *,
    hook_goal: str = "",
    intent: str = "",
    topic_context: str = "",
    theme_context: str = "",
    api_key: str | None = None,
    model: str = "",
    claude_api_key: str | None = None,
    claude_model: str = "",
    provider: str = "auto",
    openrouter_api_key: str | None = None,
    openrouter_model: str = "",
    openrouter_base_url: str = "",
) -> tuple[list[dict[str, Any]], str]:
    """Craft Instagram slide lines from spoken seeds + yellow highlights.

    Prefers Claude when configured. Returns ``(slides, provider)``.
    Invented numbers fall back to the spoken seed. On LLM failure, keeps
    original lines and applies heuristic highlights so the UI never drops
    yellow emphasis entirely.
    """
    if not slides:
        return slides, "none"

    payload = []
    for i, slide in enumerate(slides):
        raw = str(
            slide.get("transcript_text")
            or slide.get("hook_line")
            or slide.get("caption")
            or slide.get("snippet")
            or ""
        ).strip()
        payload.append({"i": i, "text": raw[:400]})

    has_llm = _llm_has_any_key(
        api_key=api_key,
        claude_api_key=claude_api_key,
        openrouter_api_key=openrouter_api_key,
        openrouter_model=openrouter_model,
    )
    used_provider = "heuristic"
    parsed_rows: list[dict[str, Any]] = []

    spoken_corpus = " ".join(str(row.get("text") or "") for row in payload)
    if has_llm:
        prompt = (
            "You write Instagram carousel SLIDE COPY for a vertical 4:5 post.\n"
            f"{_SLIDE_CRAFT_BRIEF}"
            "CRAFT RULES:\n"
            "- Rewrite EACH input into a complete, self-contained 1–2 line clause "
            "(roughly 6–16 words; hard maximum 18).\n"
            "- Do NOT paste the transcript. Drop filler, [music], and mid-clause scraps.\n"
            "- Keep the same slide count and order as the input (same i).\n"
            f"- Chosen theme (hard boundary): "
            f"{(theme_context or '').strip()[:240] or '(none)'}\n"
            f"- Chosen topic (the ONE argument for this deck): "
            f"{(topic_context or '').strip()[:240] or '(none)'}\n"
            f"- Selected performance hook (place once where it performs best): "
            f"{(hook_goal or '').strip()[:240] or '(none)'}\n"
            f"- Directional intent: {(intent or '').strip()[:240] or '(none)'}\n"
            "- Before writing, silently reject any seed that introduces another tactic "
            "or subject. If a weak seed cannot advance the chosen topic, bridge it back "
            "to the topic without inventing facts.\n"
            "- Pick 1–3 words per slide to highlight in yellow "
            "(key nouns/verbs/names/numbers).\n"
            "- highlight = 0-based word indices into the returned crafted text "
            "after whitespace split\n"
            "- Never highlight every word; never return empty highlight\n"
            "Return ONLY JSON:\n"
            '{"slides":[{"i":0,"text":"...","highlight":[1,2],"highlight_words":["AI","crisis"]}]}\n\n'
            f"Spoken seeds JSON:\n{json.dumps(payload, ensure_ascii=False)}"
        )
        try:
            text, used_provider = await _llm_complete_json(
                prompt=prompt,
                system=(
                    "You are an expert Instagram carousel copywriter. "
                    "Return ONLY valid JSON. Craft complete swipeable lines; "
                    "yellow highlights must be sparse and punchy."
                ),
                temperature=0.35,
                max_tokens=3500,
                api_key=api_key,
                model=model,
                claude_api_key=claude_api_key,
                claude_model=claude_model or "claude-sonnet-4-5-20250929",
                provider=provider,
                openrouter_api_key=openrouter_api_key,
                openrouter_model=openrouter_model,
                openrouter_base_url=openrouter_base_url,
            )
            m = re.search(r"\{[\s\S]*\}", text or "")
            data = json.loads(m.group() if m else text or "{}")
            rows = data.get("slides") if isinstance(data, dict) else None
            if isinstance(rows, list):
                parsed_rows = [r for r in rows if isinstance(r, dict)]
        except Exception as exc:  # noqa: BLE001
            logger.warning("instagram copy polish failed (%s): %s", used_provider, str(exc)[:180])
            used_provider = "heuristic"
            parsed_rows = []

    by_i = {
        int(r["i"]): r
        for r in parsed_rows
        if r.get("i") is not None
        and str(r.get("i")).lstrip("-").isdigit()
    }

    def _norm_words(text: str) -> list[str]:
        return [
            w.lower().strip(".,!?;:\"'()[]")
            for w in re.split(r"\s+", (text or "").strip())
            if w
        ]

    def _is_exact_or_subset(candidate: str, seed: str) -> bool:
        """True when candidate is the seed (or a contiguous subset of seed words)."""
        c = _norm_words(candidate)
        s = _norm_words(seed)
        if not c or not s:
            return False
        if c == s:
            return True
        # Allow punctuation/casing-only edits: same tokens.
        if " ".join(c) == " ".join(s):
            return True
        # Contiguous subset of seed (trim only) — never invented tokens.
        if len(c) <= len(s):
            joined_s = " ".join(s)
            joined_c = " ".join(c)
            if joined_c in joined_s:
                return True
        return False

    out: list[dict[str, Any]] = []
    for i, slide in enumerate(slides):
        item = dict(slide)
        seed = str(
            item.get("transcript_text")
            or item.get("hook_line")
            or item.get("caption")
            or item.get("snippet")
            or ""
        ).strip()
        row = by_i.get(i) or {}
        polished = " ".join(str(row.get("text") or seed).split()).strip()[:220]
        slide_provider = used_provider
        word_count = len(polished.split()) if polished else 0
        # Allow crafted rewrites; reject invented numbers or empty/endless lines.
        if seed and polished and (
            not _hook_numbers_are_grounded(polished, spoken_corpus or seed)
            or not 4 <= word_count <= 18
        ):
            polished = seed
            slide_provider = (
                f"{used_provider}+transcript_locked"
                if used_provider != "heuristic"
                else "heuristic"
            )
        if not polished:
            polished, indices, hl_words = _heuristic_highlight_for_line(seed)
        else:
            indices, hl_words = _normalize_highlight_indices(
                polished,
                row.get("highlight") or row.get("highlights"),
                row.get("highlight_words") or row.get("yellow_words"),
            )
            if not indices:
                _, indices, hl_words = _heuristic_highlight_for_line(polished)
        if seed:
            item.setdefault("original_text", seed[:400])
        item["hook_line"] = polished
        item["transcript_text"] = polished
        item["caption"] = polished
        item["snippet"] = polished
        item["highlight"] = indices
        item["highlight_words"] = hl_words
        item["copy_source"] = slide_provider
        out.append(item)

    # Deterministic MU-style guard: if the transcript genuinely contains a
    # sacred action but the rewrite removed every such verb, restore one
    # supported seed verb rather than inventing an action. Prefer cover/payoff
    # positions, then the earliest supporting slide.
    if out and not any(
        mu_sacred_action_words(str(slide.get("hook_line") or "")) for slide in out
    ):
        supported = [
            i
            for i, row in enumerate(payload)
            if mu_sacred_action_words(str(row.get("text") or ""))
        ]
        if supported:
            preferred = next(
                (i for i in (0, len(out) - 1) if i in supported),
                supported[0],
            )
            seed = str(payload[preferred].get("text") or "").strip()
            grounded = _grounded_mu_action_clause(seed)
            if grounded:
                out[preferred]["hook_line"] = grounded
                out[preferred]["transcript_text"] = grounded
                out[preferred]["caption"] = grounded
                out[preferred]["snippet"] = grounded
                _unused, indices, words = _heuristic_highlight_for_line(grounded)
                sacred_norm = {
                    word.lower().strip(".,!?;:\"'()[]")
                    for word in mu_sacred_action_words(grounded)
                }
                seed_words = grounded.split()
                sacred_indices = [
                    i
                    for i, word in enumerate(seed_words)
                    if word.lower().strip(".,!?;:\"'()[]") in sacred_norm
                ]
                out[preferred]["highlight"] = (sacred_indices or indices)[:3]
                out[preferred]["highlight_words"] = [
                    seed_words[i] for i in out[preferred]["highlight"]
                ] or words
                out[preferred]["copy_source"] = (
                    f"{out[preferred].get('copy_source') or used_provider}"
                    "+mu_action_grounded"
                )
    for slide in out:
        slide["mu_action_verb_used"] = bool(
            mu_sacred_action_words(str(slide.get("hook_line") or ""))
        )
    return out, used_provider


async def finalize_carousels_instagram_copy(
    carousels: list[dict[str, Any]],
    *,
    intent: str = "",
    api_key: str | None = None,
    model: str = "",
    claude_api_key: str | None = None,
    claude_model: str = "",
    provider: str = "auto",
    openrouter_api_key: str | None = None,
    openrouter_model: str = "",
    openrouter_base_url: str = "",
) -> tuple[list[dict[str, Any]], str]:
    """Polish every carousel's slides (Claude-preferred) and return provider used."""
    if not carousels:
        return carousels, "none"
    provider_used = "none"
    out: list[dict[str, Any]] = []
    for car in carousels:
        item = dict(car)
        slides = list(item.get("slides") or [])
        hook_goal = str(item.get("hook_goal") or (item.get("hooks") or [""])[0] or "")
        topic_context = str(
            item.get("topic_context") or (item.get("topics") or [""])[0] or ""
        )
        theme_context = str(item.get("theme_context") or "")
        polished, used = await polish_slides_instagram_copy(
            slides,
            hook_goal=hook_goal,
            intent=intent,
            topic_context=topic_context,
            theme_context=theme_context,
            api_key=api_key,
            model=model,
            claude_api_key=claude_api_key,
            claude_model=claude_model,
            provider=provider,
            openrouter_api_key=openrouter_api_key,
            openrouter_model=openrouter_model,
            openrouter_base_url=openrouter_base_url,
        )
        item["slides"] = polished
        item["slide_count"] = len(polished)
        item["copy_source"] = used
        provider_used = used if provider_used == "none" else provider_used
        if used == "openrouter":
            provider_used = "openrouter"
        elif used == "claude":
            provider_used = "claude"
        out.append(item)
    return out, provider_used


_CAROUSEL_QUALITY_STOPWORDS = frozenset(
    {
        "a", "an", "and", "are", "as", "at", "be", "but", "by", "for", "from",
        "has", "have", "i", "in", "is", "it", "of", "on", "or", "our", "that",
        "the", "this", "to", "was", "we", "were", "with", "you", "your",
    }
)


def _carousel_quality_text(slide: dict[str, Any]) -> str:
    return " ".join(
        str(
            slide.get("transcript_text")
            or slide.get("hook_line")
            or slide.get("caption")
            or slide.get("snippet")
            or ""
        ).split()
    ).strip()


def _carousel_idea_tokens(text: str) -> set[str]:
    return {
        token
        for token in re.findall(r"[a-z0-9']+", (text or "").lower())
        if len(token) > 2 and token not in _CAROUSEL_QUALITY_STOPWORDS
    }


def _carousel_idea_similarity(left: str, right: str) -> float:
    a = _carousel_idea_tokens(left)
    b = _carousel_idea_tokens(right)
    if not a or not b:
        return 0.0
    return len(a & b) / max(1, min(len(a), len(b)))


def find_duplicate_slide_pairs(
    slides: list[dict[str, Any]],
    *,
    threshold: float = 0.8,
) -> list[list[int]]:
    """Return later-slide duplicate pairs ``[left, right]`` using idea overlap."""
    texts = [_carousel_quality_text(slide) for slide in slides]
    pairs: list[list[int]] = []
    for right in range(1, len(texts)):
        for left in range(right):
            if texts[left] and texts[right] and _carousel_idea_similarity(
                texts[left], texts[right]
            ) >= threshold:
                pairs.append([left, right])
                break
    return pairs


def _source_safe_concise_text(text: str, *, max_words: int = 24) -> str:
    """Return a contiguous source substring, never newly authored copy."""
    cleaned = " ".join((text or "").split()).strip()
    words = cleaned.split()
    if len(words) <= max_words and len(cleaned) <= 180:
        return cleaned
    # A complete leading sentence/clause is safe because it is an exact,
    # contiguous substring of the transcript-verified display text.
    for match in re.finditer(r"[.!?;:](?:\s|$)", cleaned):
        candidate = cleaned[: match.end()].strip()
        count = len(candidate.split())
        if 5 <= count <= max_words and len(candidate) <= 180:
            return candidate
    return cleaned


def score_and_repair_carousel_quality(
    carousel: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Deterministically score carousel quality and apply source-safe repairs.

    Timestamps and source fields are never changed. Copy repairs can only
    normalize whitespace or select a complete contiguous prefix of existing
    slide text, so this pass cannot introduce a factual claim.
    """
    item = dict(carousel)
    slides = [dict(slide) for slide in (item.get("slides") or []) if isinstance(slide, dict)]
    repairs: list[str] = []

    for index, slide in enumerate(slides):
        original = _carousel_quality_text(slide)
        concise = _source_safe_concise_text(original)
        if concise != original:
            repairs.append(f"slide_{index + 1}:concise_prefix")
        if concise:
            for field in ("hook_line", "transcript_text", "caption", "snippet"):
                slide[field] = concise
        indices, words = _normalize_highlight_indices(
            concise,
            slide.get("highlight"),
            slide.get("highlight_words"),
        )
        slide["highlight"] = indices
        slide["highlight_words"] = words

    extra = [
        str(repair)
        for repair in (item.get("duplicate_repairs") or [])
        if str(repair).strip()
    ]
    repairs.extend(extra)

    texts = [_carousel_quality_text(slide) for slide in slides]
    word_counts = [len(text.split()) for text in texts]
    duplicate_pairs = find_duplicate_slide_pairs(slides)

    cover = texts[0] if texts else ""
    cover_words = word_counts[0] if word_counts else 0
    cover_score = 35
    if 4 <= cover_words <= 16:
        cover_score += 35
    elif cover_words:
        cover_score += 15
    if re.search(r"[?!]|\b\d+(?:\.\d+)?%?\b", cover):
        cover_score += 20
    if len(_carousel_idea_tokens(cover)) >= 3:
        cover_score += 10
    cover_score = min(100, cover_score if cover else 0)

    readable = sum(
        1
        for text, count in zip(texts, word_counts)
        if text and count <= 24 and len(text) <= 180
    )
    readability_score = round(100 * readable / max(1, len(slides)))

    monotonic_pairs = 0
    progressive_pairs = 0
    for left, right in zip(slides, slides[1:]):
        try:
            monotonic = float(right.get("timestamp_sec") or 0) >= float(
                left.get("timestamp_sec") or 0
            )
        except (TypeError, ValueError):
            monotonic = False
        if monotonic:
            monotonic_pairs += 1
        if _carousel_idea_similarity(_carousel_quality_text(left), _carousel_quality_text(right)) < 0.8:
            progressive_pairs += 1
    pair_count = max(1, len(slides) - 1)
    progression_score = round(
        100 * (0.6 * monotonic_pairs + 0.4 * progressive_pairs) / pair_count
    )
    if len(slides) <= 1:
        progression_score = 0

    duplicate_score = round(
        100 * (1.0 - len(duplicate_pairs) / max(1, len(slides) - 1))
    )
    duplicate_score = max(0, duplicate_score)

    ending = texts[-1] if texts else ""
    ending_score = 30 if ending else 0
    if ending and len(ending.split()) <= 20:
        ending_score += 25
    if re.search(r"[.!?]$", ending):
        ending_score += 20
    if re.search(
        r"\b(remember|try|start|build|choose|ask|learn|save|share|follow|next|result|because|so)\b",
        ending,
        re.IGNORECASE,
    ):
        ending_score += 25
    ending_score = min(100, ending_score)

    dimensions = {
        "cover_strength": cover_score,
        "swipe_progression": progression_score,
        "copy_readability": readability_score,
        "idea_uniqueness": duplicate_score,
        "ending_payoff": ending_score,
    }
    overall = round(sum(dimensions.values()) / len(dimensions))
    issues: list[str] = []
    if cover_score < 70:
        issues.append("weak_cover")
    if progression_score < 75:
        issues.append("weak_swipe_progression")
    if readability_score < 90:
        issues.append("dense_copy")
    if duplicate_pairs:
        issues.append("duplicate_ideas")
    if ending_score < 70:
        issues.append("weak_ending_payoff")

    report: dict[str, Any] = {
        "score": overall,
        "dimensions": dimensions,
        "issues": issues,
        "repairs": repairs,
        "duplicate_pairs": duplicate_pairs,
        "grounding": "transcript_locked",
    }
    item["slides"] = slides
    item["slide_count"] = len(slides)
    item["quality_report"] = report
    return item, report


def apply_carousel_quality_pass(
    carousels: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Apply deterministic quality scoring/repair and return a compact rollup."""
    repaired: list[dict[str, Any]] = []
    reports: list[dict[str, Any]] = []
    for carousel in carousels:
        item, report = score_and_repair_carousel_quality(carousel)
        repaired.append(item)
        reports.append(report)
    scores = [int(report.get("score") or 0) for report in reports]
    summary = {
        "carousel_count": len(reports),
        "average_score": round(sum(scores) / len(scores)) if scores else 0,
        "needs_attention": sum(1 for score in scores if score < 70),
        "issue_count": sum(len(report.get("issues") or []) for report in reports),
        "repair_count": sum(len(report.get("repairs") or []) for report in reports),
        "algorithm": "deterministic_transcript_locked_v2",
    }
    return repaired, summary
