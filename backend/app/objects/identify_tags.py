"""Qwen identify labels: objects and actions, each with synonyms.

Used by the isolated /search/testv2 demo. Production /search does not read this
until that path is promoted.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Iterable

QWEN_IDENTIFY_MODEL_VERSION = "qwen3vl-identify-v1"

IDENTIFY_PROMPT = """You are labeling a photo for a visual search index.

Return two sections. Keep objects and actions separate.
Each line is: primary label | synonym, synonym, synonym
Synonyms are other phrases a person might type to find the same thing.

OBJECTS
Specific nouns a person could search for:
- garments with color and type, e.g. black shirt | dark tee, black t-shirt
- brand, logo, or sign text copied exactly, e.g. Flourish Foods | flourish
- named objects, e.g. ceremonial cheque | check, oversized check, award cheque
- people as a short description, e.g. man with beard | bearded man

ACTIONS
Visible acts as verb + object/scene, never a bare gerund:
- giving ceremonial cheque | handing cheque, presenting cheque, awarding cheque
- wearing navy blazer | dressed in navy blazer
- cooking at stove | chopping vegetables, preparing food
- speaking on stage | presenting on stage, giving a talk

Never list isolated body parts: eye, mouth, nose, ear, hair, head, face, teeth, skin, smile, hand, arm, leg, foot.
Never list empty scene filler: wall, floor, ceiling, sky, light, shadow, background, text, logo, sign — unless the sign text is readable.
Never list a bare verb alone: holding, wearing, sitting, smiling, standing.

No bounding boxes. No sentences. Skip anything you cannot identify specifically."""

# Query-time action synonyms so "giving cheque" also hits handing/presenting.
ACTION_SYNONYM_GROUPS: tuple[frozenset[str], ...] = (
    frozenset({"give", "giving", "hand", "handing", "present", "presenting", "award", "awarding"}),
    frozenset({"hold", "holding", "carry", "carrying"}),
    frozenset({"wear", "wearing", "wears", "dressed"}),
    frozenset({
        "cook", "cooking", "chop", "chopping", "grill", "grilling",
        "bake", "baking", "fry", "frying", "prepare", "preparing",
        "chef",
    }),
    frozenset({"eat", "eating", "dining"}),
    frozenset({"dance", "dancing"}),
    frozenset({"talk", "talking", "speak", "speaking", "chat", "chatting", "discuss", "discussing"}),
    frozenset({"study", "studying"}),
    frozenset({"work", "working"}),
    frozenset({"lift", "lifting"}),
    frozenset({"row", "rowing"}),
    frozenset({"run", "running", "jog", "jogging"}),
    frozenset({"exercise", "exercising", "workout", "training"}),
    frozenset({"pose", "posing"}),
    frozenset({"celebrate", "celebrating", "clap", "clapping"}),
    frozenset({"graduate", "graduating", "graduation"}),
)

# Object aliases used only on the testv2 identify path (not production taxonomy).
OBJECT_SYNONYM_GROUPS: tuple[frozenset[str], ...] = (
    frozenset({"cheque", "cheques", "check", "checks"}),
    frozenset({"trophy", "trophies", "award cup"}),
    frozenset({"plaque", "award plaque"}),
    frozenset({"certificate", "certificates", "diploma"}),
    frozenset({"microphone", "mic", "shure", "shure microphone"}),
    frozenset({"tshirt", "t-shirt", "tee", "t shirt"}),
)

_ACTION_STEMS = frozenset().union(*ACTION_SYNONYM_GROUPS)
_OBJECT_STOP = frozenset({
    "a", "an", "the", "to", "for", "of", "on", "in", "at", "with", "and",
    "student", "students", "people", "person", "photo", "photos", "image",
    "show", "find",
})

JUNK = {
    "remove", "reveal", "extrude", "assemble", "modern", "lesson", "clip art",
    "team presentation", "press room", "number icon", "frame", "head", "smile",
    "hair", "sun", "sky", "horizon", "sunlight", "eye", "eyes", "mouth", "nose",
    "ear", "ears", "teeth", "tooth", "lip", "lips", "skin", "face", "eyebrow",
    "eyebrows", "cheek", "cheeks", "chin", "forehead", "neck", "hand", "hands",
    "finger", "fingers", "arm", "arms", "leg", "legs", "foot", "feet", "wrist",
    "palm", "wall", "walls", "floor", "ceiling", "light", "lights", "lighting",
    "shadow", "shadows", "background", "black background", "white background",
    "sleeve", "sleeves", "collar", "button", "buttons", "hem", "stitch", "thread",
    "fabric", "cotton", "denim", "pocket", "pockets", "zipper", "hole", "sign",
    "logo", "text", "paper", "shirt", "t-shirt", "tshirt", "tee", "pants", "jeans",
    "jacket", "hat", "shorts", "clothes", "clothing", "garment", "apparel", "chair",
    "table", "window", "door", "screen", "banner", "poster", "bag", "cup",
    "person", "people", "man", "woman", "boy", "girl", "image", "photo", "picture",
}

_COLORS = {
    "black", "white", "red", "blue", "navy", "green", "yellow", "pink", "brown",
    "beige", "grey", "gray", "orange", "purple", "gold", "silver",
}
_VERBISH = re.compile(
    r"^(sitting|standing|wearing|holding|looking|smiling|walking|running|"
    r"posing|showing|using|playing|giving|handing)$"
)
_LINE = re.compile(r"^[*\-•\d.\s]+")
_BODY_WORD = re.compile(
    r"\b(eye|eyes|mouth|nose|ear|ears|teeth|lips?|skin|face|hair|head|"
    r"smile|cheek|chin|forehead|neck|hands?|fingers?|arms?|legs?|feet|foot)\b"
)
_SECTION = re.compile(r"^(objects?|actions?)\s*:?\s*$", re.IGNORECASE)


@dataclass(frozen=True)
class IdentifyLabel:
    kind: str  # "object" | "action"
    label: str
    synonyms: tuple[str, ...] = ()


@dataclass
class IdentifyResult:
    objects: list[IdentifyLabel] = field(default_factory=list)
    actions: list[IdentifyLabel] = field(default_factory=list)

    @property
    def merged(self) -> list[IdentifyLabel]:
        """Separate lanes concatenated: objects first, then actions, de-duped."""
        seen: set[tuple[str, str]] = set()
        out: list[IdentifyLabel] = []
        for item in (*self.objects, *self.actions):
            key = (item.kind, item.label)
            if key in seen:
                continue
            seen.add(key)
            out.append(item)
        return out


@dataclass(frozen=True)
class IdentifyQuery:
    object_terms: frozenset[str]
    action_terms: frozenset[str]

    @property
    def lookup_labels(self) -> frozenset[str]:
        return self.object_terms | self.action_terms

    @property
    def requires_both(self) -> bool:
        return bool(self.object_terms) and bool(self.action_terms)


def _clean_phrase(value: str) -> str:
    label = _LINE.sub("", value or "").strip().strip("*").strip()
    label = re.sub(r"\s+", " ", label).strip(" .,:;").casefold()
    return label


def _accept_phrase(label: str, *, allow_verb_object: bool) -> bool:
    if not label or len(label) > 48:
        return False
    if any(ch.isdigit() for ch in label) and re.search(r"\d{2,}", label):
        return False
    if label in JUNK or label in _COLORS:
        return False
    if _VERBISH.match(label):
        return False
    if _BODY_WORD.search(label) and len(label.split()) <= 2:
        return False
    if not allow_verb_object and len(label.split()) == 1 and label in _ACTION_STEMS:
        return False
    return True


def filter_tags(text: str) -> list[str]:
    """Legacy noun-only filter. Kept so existing identify tests stay green."""
    seen: set[str] = set()
    out: list[str] = []
    for raw in (text or "").splitlines():
        label = _clean_phrase(raw)
        if not _accept_phrase(label, allow_verb_object=False):
            continue
        if label in seen:
            continue
        seen.add(label)
        out.append(label)
    return out[:24]


def _split_primary_synonyms(line: str) -> tuple[str, tuple[str, ...]]:
    raw = _LINE.sub("", line).strip().strip("*").strip()
    if "|" in raw:
        primary, rest = raw.split("|", 1)
    elif ":" in raw and not raw.lower().startswith("http"):
        primary, rest = raw.split(":", 1)
    else:
        primary, rest = raw, ""
    label = _clean_phrase(primary)
    syns: list[str] = []
    seen = {label} if label else set()
    for part in re.split(r"[,;/]", rest):
        syn = _clean_phrase(part)
        if not syn or syn in seen:
            continue
        seen.add(syn)
        syns.append(syn)
    return label, tuple(syns)


def parse_identify_output(text: str) -> IdentifyResult:
    """Parse OBJECTS / ACTIONS sections. Unsectioned lines are treated as objects."""
    objects: list[IdentifyLabel] = []
    actions: list[IdentifyLabel] = []
    section = "object"
    seen: set[tuple[str, str]] = set()

    for raw in (text or "").splitlines():
        stripped = _LINE.sub("", raw).strip().strip("*").strip()
        if not stripped or stripped.startswith("```"):
            continue
        heading = _SECTION.match(stripped)
        if heading:
            section = "action" if heading.group(1).lower().startswith("action") else "object"
            continue
        label, synonyms = _split_primary_synonyms(stripped)
        allow = section == "action"
        if not _accept_phrase(label, allow_verb_object=allow):
            continue
        kept_syns = tuple(
            syn for syn in synonyms if _accept_phrase(syn, allow_verb_object=allow)
        )
        key = (section, label)
        if key in seen:
            continue
        seen.add(key)
        item = IdentifyLabel(kind=section, label=label, synonyms=kept_syns)
        if section == "action":
            actions.append(item)
        else:
            objects.append(item)
        if len(objects) + len(actions) >= 32:
            break
    return IdentifyResult(objects=objects, actions=actions)


def merge_identify_lanes(
    objects: list[IdentifyLabel] | None = None,
    actions: list[IdentifyLabel] | None = None,
) -> IdentifyResult:
    """Keep lanes separate inside IdentifyResult; `.merged` concatenates them."""
    return IdentifyResult(objects=list(objects or []), actions=list(actions or []))


def _expand_group(token: str, groups: tuple[frozenset[str], ...]) -> frozenset[str]:
    for group in groups:
        if token in group:
            return group
    return frozenset({token})


def parse_identify_query(query: str) -> IdentifyQuery:
    tokens = tuple(re.findall(r"[a-z0-9]+", (query or "").casefold()))
    action_terms: set[str] = set()
    object_terms: set[str] = set()
    for token in tokens:
        if token in _ACTION_STEMS:
            action_terms |= _expand_group(token, ACTION_SYNONYM_GROUPS)
        elif token not in _OBJECT_STOP:
            object_terms |= _expand_group(token, OBJECT_SYNONYM_GROUPS)
            object_terms.add(token)
    # Compact forms: tshirt ↔ t-shirt tokens already split; add joined leftovers.
    compact = "".join(t for t in tokens if t not in _OBJECT_STOP and t not in _ACTION_STEMS)
    if compact and len(compact) >= 4:
        object_terms.add(compact)
        object_terms |= _expand_group(compact, OBJECT_SYNONYM_GROUPS)
    return IdentifyQuery(frozenset(object_terms), frozenset(action_terms))


def phrases_for_index(item: IdentifyLabel) -> tuple[str, ...]:
    """Primary + synonyms + tokens so inverted lookup can hit any phrasing."""
    seen: set[str] = set()
    out: list[str] = []
    for phrase in (item.label, *item.synonyms):
        if phrase and phrase not in seen:
            seen.add(phrase)
            out.append(phrase)
        for token in re.findall(r"[a-z0-9]+", phrase):
            if token in _OBJECT_STOP or len(token) < 3:
                continue
            if item.kind == "action" and token not in _ACTION_STEMS and token in _COLORS:
                continue
            if token not in seen:
                seen.add(token)
                out.append(token)
    return tuple(out)


def persist_rows(parsed: IdentifyResult) -> list[dict[str, object]]:
    """Rows for media_identify_labels: primary, synonyms, and tokens. Separate lanes."""
    rows: list[dict[str, object]] = []
    seen: set[str] = set()
    for item in parsed.merged:
        source = "qwen_action" if item.kind == "action" else "qwen_identify"
        category = "action" if item.kind == "action" else "object"
        phrases = phrases_for_index(item)
        for index, phrase in enumerate(phrases):
            if phrase in seen:
                continue
            seen.add(phrase)
            rows.append(
                {
                    "canonical_label": phrase[:96],
                    "category": category,
                    "confidence": 0.92 if index == 0 else 0.8,
                    "evidence_source": source if index == 0 else "qwen_synonym",
                    "evidence_text": item.label if index else f"synonym of {item.label}",
                    "hit_count": 1,
                    "model_version": QWEN_IDENTIFY_MODEL_VERSION,
                }
            )
    return rows


def file_covers_identify_query(labels: Iterable[str], query: IdentifyQuery) -> bool:
    haystack = {str(label).casefold() for label in labels if label}
    if query.action_terms and not (haystack & query.action_terms):
        return False
    if query.object_terms and not (haystack & query.object_terms):
        return False
    return bool(query.lookup_labels)


# Scene/role filler that must not be required as conjunctive caption tokens.
_EXPERIMENTAL_OBJECT_FILLER = frozenset({
    "campus", "food", "race", "backdrop", "background", "ceremony",
    "stage", "over", "into", "during", "show", "find",
})
_WEAK_OBJECT_MODIFIERS = frozenset({
    "open", "closed", "large", "small", "big", "huge", "tiny",
    "empty", "full", "next", "first", "last", "new", "old",
})
_EXPERIMENTAL_RARE = frozenset({"hyrox", "cheque", "check", "shure"})
_CHEQUE_TERMS = frozenset({"cheque", "cheques", "check", "checks"})
_EXPERIMENTAL_GARMENTS = frozenset({
    "blazer", "jacket", "suit", "sari", "dress", "shirt", "tshirt",
})
# "checking" → "check" is a false friend of ceremonial cheque.
_NO_ING_OBJECT_STEM = frozenset({"checking", "checked"})
_GUEST_CHECK_RE = re.compile(r"\bguest\s+checks?\b", re.IGNORECASE)
_CHECKIN_RE = re.compile(
    r"\bcheck-ins?\b|\bcheckins?\b|\bchecking[\s-]+in\b"
    r"|\bcheck[\s-]+in(?!\s+front)\b",
    re.IGNORECASE,
)
_CEREMONIAL_CHEQUE_RE = re.compile(
    r"\bcheques?\b"
    r"|\b(?:prize|novelty|ceremonial|oversized|jumbo|giant|large|mock|award|"
    r"promotional|winner|donation|national|paper)\s+(?:['\"\w-]+\s+){0,3}checks?\b"
    r"|\b(?:holding|handing|presenting|giving|awarding|receiving|holds|hold)\s+"
    r"(?:['\"\w-]+\s+){0,8}checks?\b"
    r"|\b(?:inr|rs\.?|rupees?|₹|lakh|lakhs)\b.{0,80}\bchecks?\b"
    r"|\bchecks?\b.{0,80}\b(?:inr|rs\.?|rupees?|₹|lakh|lakhs|payable)\b",
    re.IGNORECASE,
)


def _distinctive_query_objects(query: str, objects: set[str]) -> set[str]:
    """Objects the caption must hit when the query named a real thing.

    Drops filler (over/stage), weak modifiers (open/large), and compacted
    leftovers like overopenflame. Keeps synonym expansions (cheque→check).
    """
    qtok = set(re.findall(r"[a-z0-9]+", (query or "").casefold()))
    out: set[str] = set()
    for token in objects:
        if token in _EXPERIMENTAL_OBJECT_FILLER or token in _WEAK_OBJECT_MODIFIERS:
            continue
        if token in qtok:
            out.add(token)
            continue
        for group in OBJECT_SYNONYM_GROUPS:
            if token in group and (qtok & group):
                out.add(token)
                break
    return out


def ceremonial_cheque_in_caption(caption: str) -> bool:
    """True for prize/novelty cheques; false for check-in desks and guest-check pads."""
    text = caption or ""
    if _GUEST_CHECK_RE.search(text):
        return False
    if _CEREMONIAL_CHEQUE_RE.search(text):
        return True
    return False


def _caption_token_set(caption: str) -> set[str]:
    tokens = set(re.findall(r"[a-z0-9]+", (caption or "").casefold()))
    extra: set[str] = set()
    for token in tokens:
        if (
            len(token) > 4
            and token.endswith("ing")
            and token not in _NO_ING_OBJECT_STEM
        ):
            extra.add(token[:-3])
        if len(token) > 3 and token.endswith("s") and not token.endswith("ss"):
            extra.add(token[:-1])
        extra |= _expand_group(token, ACTION_SYNONYM_GROUPS)
        extra |= _expand_group(token, OBJECT_SYNONYM_GROUPS)
    if tokens & _EXPERIMENTAL_GARMENTS:
        extra.update({"wear", "wearing", "wears", "dressed"})
    if {"chef", "kitchen", "stove"} & tokens:
        extra.update({"cook", "cooking", "prepare", "preparing", "chef"})
    if {"novelty", "ceremonial", "oversized"} & tokens and (
        {"check", "cheque"} & tokens or {"check", "cheque"} & extra
    ):
        extra.update({"give", "giving", "hand", "handing", "hold", "holding"})
    return tokens | extra


def experimental_evidence_score(caption: str, query: str) -> float:
    """Rank caption support for /search/testv2. Zero means no overlap; never AND-gates.

    A cooking photo that only says kitchen, or a cheque photo that only says
    novelty check, still scores above zero. Full action+object overlap ranks higher.
    """
    parsed = parse_identify_query(query)
    objects = parsed.object_terms - _EXPERIMENTAL_OBJECT_FILLER
    if objects & _CHEQUE_TERMS and not ceremonial_cheque_in_caption(caption):
        return 0.0
    hay = _caption_token_set(caption)
    actions = parsed.action_terms
    distinctive = _distinctive_query_objects(query, objects)
    if distinctive and not (hay & distinctive):
        return 0.0
    if not actions and not objects:
        return 0.0
    action_hit = bool(hay & actions) if actions else False
    # Query named an action and a real object: a logo that only mentions
    # the object (Novartis "flame-like icon") is not a hit. Prize-cheque
    # stills without a giving verb are still cheques.
    if distinctive and actions and not action_hit:
        cheque_ok = bool(objects & _CHEQUE_TERMS) and ceremonial_cheque_in_caption(caption)
        if not cheque_ok:
            return 0.0
    object_overlap = hay & objects if objects else set()
    object_hit = bool(object_overlap)
    if not action_hit and not object_hit:
        return 0.0
    score = 0.0
    if action_hit:
        score += 0.42
    if object_hit:
        coverage = len(object_overlap) / max(1, len(objects))
        score += 0.38 + 0.12 * coverage
    if object_overlap & _EXPERIMENTAL_RARE:
        score += 0.10
    if action_hit and object_hit:
        score += 0.12
    return min(1.0, score)


def experimental_caption_match(caption: str, query: str) -> bool:
    """True when the caption shares any distinctive object or action with the query."""
    return experimental_evidence_score(caption, query) > 0.0


def labels_from_legacy_tags(tags: Iterable[str], raw_text: str | None = None) -> IdentifyResult:
    """Map the 100-image noun-only JSON into separate object/action lanes."""
    parsed = parse_identify_output(raw_text or "")
    if parsed.objects or parsed.actions:
        return parsed
    objects: list[IdentifyLabel] = []
    actions: list[IdentifyLabel] = []
    for tag in tags:
        label = _clean_phrase(str(tag))
        if not label:
            continue
        first = label.split()[0]
        if first in _ACTION_STEMS and len(label.split()) >= 2:
            if _accept_phrase(label, allow_verb_object=True):
                actions.append(IdentifyLabel(kind="action", label=label))
        elif _accept_phrase(label, allow_verb_object=False):
            objects.append(IdentifyLabel(kind="object", label=label))
    return IdentifyResult(objects=objects, actions=actions)


EXPERIMENTAL_OVERLAY_NAME = "experimental_overlay.json"


def identify_result_from_row(row: dict) -> IdentifyResult:
    """Prefer stored object/action lanes; fall back to raw text or noun tags."""
    objects_raw = row.get("objects") or []
    actions_raw = row.get("actions") or []
    if objects_raw or actions_raw:
        objects = [
            IdentifyLabel(
                kind="object",
                label=str(item.get("label") or "").strip(),
                synonyms=tuple(
                    str(syn).strip()
                    for syn in (item.get("synonyms") or [])
                    if str(syn).strip()
                ),
            )
            for item in objects_raw
            if str(item.get("label") or "").strip()
        ]
        actions = [
            IdentifyLabel(
                kind="action",
                label=str(item.get("label") or "").strip(),
                synonyms=tuple(
                    str(syn).strip()
                    for syn in (item.get("synonyms") or [])
                    if str(syn).strip()
                ),
            )
            for item in actions_raw
            if str(item.get("label") or "").strip()
        ]
        if objects or actions:
            return IdentifyResult(objects=objects, actions=actions)
    return labels_from_legacy_tags(row.get("tags") or [], row.get("raw_text"))


def overlay_from_payload(payload: dict) -> dict[str, IdentifyResult]:
    overlay: dict[str, IdentifyResult] = {}
    for row in payload.get("results") or []:
        fid = str(row.get("drive_file_id") or "").strip()
        if not fid:
            continue
        overlay[fid] = identify_result_from_row(row)
    return overlay


def load_local_identify_overlay(results_dir: Path | None = None) -> dict[str, IdentifyResult]:
    """Optional demo overlay. Prefers production-hit experimental JSON.

    Never written to Postgres. Production /search does not read this.
    """
    root = results_dir or (
        Path(__file__).resolve().parents[3] / "runpod" / "qwen-vl" / "results"
    )
    if not root.is_dir():
        return {}
    preferred = root / EXPERIMENTAL_OVERLAY_NAME
    files = [preferred] if preferred.is_file() else sorted(root.glob("qwen_sglang_identify_*.json"))
    if not files:
        return {}
    try:
        payload = json.loads(files[-1].read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    return overlay_from_payload(payload)


def rerank_shortlist_with_identify(
    query: str,
    files: list[dict],
    overlay: dict[str, IdentifyResult],
) -> list[dict]:
    """Re-rank a production shortlist using Qwen object/action labels only.

    Production order is preserved inside the match and non-match groups.
    """
    parsed_q = parse_identify_query(query)
    matched: list[dict] = []
    unmatched: list[dict] = []
    for item in files:
        fid = str(item.get("drive_file_id") or "").strip()
        parsed = overlay.get(fid)
        phrases = (
            [str(row["canonical_label"]) for row in persist_rows(parsed)]
            if parsed is not None
            else []
        )
        hit = bool(parsed_q.lookup_labels) and file_covers_identify_query(phrases, parsed_q)
        row = {
            **item,
            "qwen_match": hit,
            "qwen_objects": [label.label for label in (parsed.objects if parsed else [])],
            "qwen_actions": [label.label for label in (parsed.actions if parsed else [])],
        }
        (matched if hit else unmatched).append(row)
    return matched + unmatched


@lru_cache(maxsize=1)
def cached_identify_overlay() -> dict[str, IdentifyResult]:
    return load_local_identify_overlay()
