"""Deterministic parsing and matching for object-scoped search queries."""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Iterable

from app.objects.taxonomy import COLORS, TAXONOMY

_CONTENT_STOPWORDS = frozenset(
    {
        "a",
        "an",
        "and",
        "are",
        "at",
        "by",
        "containing",
        "featuring",
        "find",
        "for",
        "from",
        "in",
        "image",
        "images",
        "media",
        "of",
        "on",
        "or",
        "person",
        "people",
        "photo",
        "photos",
        "picture",
        "pictures",
        "show",
        "showing",
        "someone",
        "the",
        "to",
        "wear",
        "wearing",
        "wears",
        "with",
    }
)

# Signage taxonomy that users often mean as "printed on the garment", not a
# separate object that must also appear in the scene.
_APPAREL_PRINT_SCAFFOLD = frozenset({"text", "logo"})

# Scene words that mean "brand on backdrop/signage", not a literal residual token
# (otherwise "black background" falsely satisfies "background with text X").
_SCENE_BRAND_SCAFFOLD = frozenset(
    {
        "background",
        "backdrop",
        "bg",
        "scene",
        "wall",
        "banner",
        "signage",
    }
)

# High-value residual brand typos seen in search.
_RESIDUAL_TYPOS = {
    "mastesunion": "mastersunion",
    "masterunion": "mastersunion",
    "mastersuinon": "mastersunion",
    "mastersunoin": "mastersunion",
}


def _correct_residual_token(token: str) -> str:
    return _RESIDUAL_TYPOS.get(token, token)


def normalized_concept_tokens(value: str | None) -> tuple[str, ...]:
    """Normalize punctuation, apostrophes, separators, and Unicode generically."""
    folded = unicodedata.normalize("NFKD", value or "").casefold()
    folded = "".join(char for char in folded if not unicodedata.combining(char))
    return tuple(re.findall(r"[a-z0-9]+", folded))


def normalized_concept_text(value: str | None) -> str:
    return " ".join(normalized_concept_tokens(value))


@dataclass(frozen=True)
class QueryConcepts:
    taxonomy_labels: tuple[str, ...]
    residual_terms: tuple[str, ...]
    require_signage_brand: bool = False

    @property
    def is_conjunctive_object_query(self) -> bool:
        return bool(self.taxonomy_labels) and (
            len(self.taxonomy_labels) + len(self.residual_terms) > 1
            or self.require_signage_brand
        )


@dataclass(frozen=True)
class _AliasSpan:
    canonical: str
    tokens: tuple[str, ...]


def _alias_spans() -> tuple[_AliasSpan, ...]:
    spans: list[_AliasSpan] = []
    seen: set[tuple[str, tuple[str, ...]]] = set()
    for taxon in TAXONOMY:
        for term in taxon.terms:
            tokens = normalized_concept_tokens(term)
            key = (taxon.name, tokens)
            if tokens and key not in seen:
                seen.add(key)
                spans.append(_AliasSpan(taxon.name, tokens))
    for color in COLORS:
        canonical = "gray" if color == "grey" else color
        tokens = normalized_concept_tokens(color)
        key = (canonical, tokens)
        if key not in seen:
            seen.add(key)
            spans.append(_AliasSpan(canonical, tokens))
    return tuple(
        sorted(
            spans,
            key=lambda item: (
                -len(item.tokens),
                -sum(len(token) for token in item.tokens),
                item.canonical,
            ),
        )
    )


_ALIASES = _alias_spans()


def _token_span_matches(
    query_tokens: tuple[str, ...],
    start: int,
    alias_tokens: tuple[str, ...],
) -> int:
    """Return consumed query token count, accepting generic compact forms."""
    width = len(alias_tokens)
    if query_tokens[start : start + width] == alias_tokens:
        return width

    alias_compact = "".join(alias_tokens)
    compact = ""
    for end in range(start, min(len(query_tokens), start + width + 1)):
        compact += query_tokens[end]
        if compact in (alias_compact, alias_compact + "s"):
            return end - start + 1
        if len(compact) > len(alias_compact):
            break
    return 0


def parse_query_concepts(query: str | None) -> QueryConcepts:
    """Split longest non-overlapping taxonomy aliases from residual content."""
    tokens = normalized_concept_tokens(query)
    occupied = [False] * len(tokens)
    matches: list[tuple[int, str]] = []

    for alias in _ALIASES:
        for start in range(len(tokens)):
            consumed = _token_span_matches(tokens, start, alias.tokens)
            if not consumed or any(occupied[start : start + consumed]):
                continue
            for index in range(start, start + consumed):
                occupied[index] = True
            matches.append((start, alias.canonical))

    labels: list[str] = []
    for _, canonical in sorted(matches):
        if canonical not in labels:
            labels.append(canonical)

    residual: list[str] = []
    scene_scaffold_hit = False
    for token, used in zip(tokens, occupied):
        if used or token in _CONTENT_STOPWORDS:
            continue
        if token in _SCENE_BRAND_SCAFFOLD:
            scene_scaffold_hit = True
            continue
        corrected = _correct_residual_token(token)
        if corrected not in residual:
            residual.append(corrected)

    # "t-shirt with text mastersunion" means brand printed on apparel, not a
    # required separate text/logo object in the scene.
    apparel = [label for label in labels if label in _APPAREL_LABELS]
    if apparel and residual:
        labels = [label for label in labels if label not in _APPAREL_PRINT_SCAFFOLD]

    # "background with text mastersunion" → brand on signage/backdrop.
    # Only when the user names a scene surface — not for bare "text mastersunion".
    require_signage_brand = bool(residual) and scene_scaffold_hit and not apparel
    if require_signage_brand:
        labels = [label for label in labels if label not in _APPAREL_PRINT_SCAFFOLD]
        if not labels:
            labels = ["sign"]

    return QueryConcepts(tuple(labels), tuple(residual), require_signage_brand)


def text_supports_concept(text: str | None, concept: str) -> bool:
    """Match concepts across punctuation, apostrophe, spaced, or compact forms."""
    haystack = normalized_concept_tokens(text)
    needle = normalized_concept_tokens(concept)
    if not haystack or not needle:
        return False
    target = "".join(needle)
    for start in range(len(haystack)):
        compact = ""
        for end in range(start, len(haystack)):
            compact += haystack[end]
            if compact in (target, target + "s"):
                return True
            if len(compact) > len(target):
                break
    return False


_APPAREL_LABELS = frozenset(
    taxon.name for taxon in TAXONOMY if taxon.category == "apparel"
)
_ASSOCIATION_LINKERS = frozenset(
    {
        "with",
        "featuring",
        "bearing",
        "printed",
        "print",
        "logo",
        "text",
        "reading",
        "reads",
        "says",
        "saying",
        "displaying",
        "showing",
        "branded",
        "matching",
        "wearing",
        "wears",
        "in",
        "a",
        "an",
        "the",
        "and",
        "black",
        "white",
        "red",
        "blue",
        "green",
        "yellow",
        "orange",
        "purple",
        "pink",
        "brown",
        "gray",
        "grey",
        "gold",
        "silver",
    }
)
_BACKDROP_MARKERS = frozenset(
    {
        "backdrop",
        "banner",
        "signage",
        "billboard",
        "poster",
        "pillar",
        "wall",
        "sign",
        "archway",
        "gateway",
        "kiosk",
        "standee",
        "installation",
    }
)


def apparel_labels_in(concepts: QueryConcepts) -> tuple[str, ...]:
    return tuple(label for label in concepts.taxonomy_labels if label in _APPAREL_LABELS)


def _concept_spans(
    haystack: tuple[str, ...],
    concept: str,
) -> list[tuple[int, int]]:
    """Return inclusive token spans where a concept appears, including compact forms."""
    needle = normalized_concept_tokens(concept)
    if not haystack or not needle:
        return []
    target = "".join(needle)
    spans: list[tuple[int, int]] = []
    for start in range(len(haystack)):
        compact = ""
        for end in range(start, len(haystack)):
            compact += haystack[end]
            if compact in (target, target + "s"):
                spans.append((start, end))
            if len(compact) > len(target):
                break
    return spans


def _apparel_spans(
    haystack: tuple[str, ...],
    apparel_labels: Iterable[str],
) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    for label in apparel_labels:
        taxon = next((item for item in TAXONOMY if item.name == label), None)
        terms = taxon.terms if taxon is not None else (label,)
        for term in terms:
            spans.extend(_concept_spans(haystack, term))
    return spans


def _residual_spans(
    haystack: tuple[str, ...],
    residual_terms: Iterable[str],
) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    residuals = [term for term in residual_terms if term]
    if not residuals:
        return []
    # Prefer the residual phrase as a unit when possible ("mastersunion").
    joined = " ".join(residuals)
    spans.extend(_concept_spans(haystack, joined))
    for term in residuals:
        spans.extend(_concept_spans(haystack, term))
    return spans


_BRAND_LINKERS = frozenset(
    {
        "with",
        "featuring",
        "bearing",
        "printed",
        "print",
        "logo",
        "text",
        "reading",
        "reads",
        "says",
        "saying",
        "displaying",
        "branded",
        "matching",
    }
)


def _span_distance(a: tuple[int, int], b: tuple[int, int]) -> int:
    a_start, a_end = a
    b_start, b_end = b
    if a_end < b_start:
        return b_start - a_end - 1
    if b_end < a_start:
        return a_start - b_end - 1
    return 0


def _apparel_in_window(
    apparel: list[tuple[int, int]],
    window_start: int,
    window_end: int,
) -> bool:
    return any(not (a_end < window_start or a_start > window_end) for a_start, a_end in apparel)


def _residual_backdrop_only(
    haystack: tuple[str, ...],
    residual: list[tuple[int, int]],
    apparel: list[tuple[int, int]],
) -> bool:
    """Brand text sits on signage and apparel is outside that local window."""
    for r_start, r_end in residual:
        local_start = max(0, r_start - 6)
        local_end = min(len(haystack) - 1, r_end + 6)
        local = haystack[local_start : local_end + 1]
        if not any(token in _BACKDROP_MARKERS for token in local):
            continue
        if not _apparel_in_window(apparel, local_start, local_end):
            return True
    return False


def _garment_brand_associated(
    apparel: list[tuple[int, int]],
    residual: list[tuple[int, int]],
    haystack: tuple[str, ...],
) -> bool:
    """True when residual brand/text is attributed to the garment phrase."""
    return apparel_brand_association_strength_from_spans(apparel, residual, haystack) > 0


def apparel_brand_association_strength_from_spans(
    apparel: list[tuple[int, int]],
    residual: list[tuple[int, int]],
    haystack: tuple[str, ...],
) -> float:
    """0..1 strength for how tightly brand text is attached to apparel."""
    best = 0.0
    for a_span in apparel:
        for r_span in residual:
            dist = _span_distance(a_span, r_span)
            a_start, a_end = a_span
            r_start, r_end = r_span
            left = min(a_start, r_start)
            right = max(a_end, r_end)
            between = haystack[left : right + 1]
            local_start = max(0, r_start - 6)
            local_end = min(len(haystack) - 1, r_end + 6)
            residual_on_signage = any(
                token in _BACKDROP_MARKERS for token in haystack[local_start : local_end + 1]
            ) and not _apparel_in_window(apparel, local_start, local_end)
            if residual_on_signage:
                continue

            # "Masters' Union t-shirt" / "HYROX tee" (touching spans)
            if dist == 0:
                best = max(best, 1.0)
                continue
            if dist <= 1:
                best = max(best, 0.96)
                continue
            if dist <= 6 and any(token in _BRAND_LINKERS for token in between):
                # Prefer printed/logo/text over generic "with"
                if any(token in {"printed", "print", "logo", "text", "branded"} for token in between):
                    best = max(best, 0.92)
                elif any(token in {"matching", "featuring"} for token in between):
                    best = max(best, 0.88)
                else:
                    best = max(best, 0.8)
                continue
            apparel_tokens = set(haystack[a_start : a_end + 1])
            residual_tokens = set(haystack[r_start : r_end + 1])
            if dist <= 4 and all(
                token in _ASSOCIATION_LINKERS
                or token in apparel_tokens
                or token in residual_tokens
                for token in between
            ):
                best = max(best, 0.72)
    return best


def apparel_brand_association_strength(
    caption: str | None,
    concepts: QueryConcepts,
    *,
    evidence_texts: Iterable[str | None] = (),
) -> float:
    """Score how clearly residual brand text is on the apparel (0 = not)."""
    apparel = apparel_labels_in(concepts)
    if not apparel or not concepts.residual_terms:
        return 0.0
    searchable = " ".join(part for part in [caption, *evidence_texts] if part)
    haystack = normalized_concept_tokens(searchable)
    apparel_spans = _apparel_spans(haystack, apparel)
    residual_spans = _residual_spans(haystack, concepts.residual_terms)
    if not apparel_spans or not residual_spans:
        return 0.0
    return apparel_brand_association_strength_from_spans(
        apparel_spans, residual_spans, haystack
    )


def residual_associated_with_apparel(
    caption: str | None,
    concepts: QueryConcepts,
    *,
    evidence_texts: Iterable[str | None] = (),
) -> bool:
    """Require residual brand/text to be linked to apparel, not only the scene."""
    apparel = apparel_labels_in(concepts)
    if not apparel or not concepts.residual_terms:
        return True
    return apparel_brand_association_strength(
        caption,
        concepts,
        evidence_texts=evidence_texts,
    ) > 0


def residual_associated_with_signage(
    caption: str | None,
    concepts: QueryConcepts,
    *,
    evidence_texts: Iterable[str | None] = (),
) -> bool:
    """Require residual brand/text to sit on backdrop/signage, not only elsewhere."""
    if not concepts.residual_terms:
        return True
    searchable = " ".join(part for part in [caption, *evidence_texts] if part)
    haystack = normalized_concept_tokens(searchable)
    residual_spans = _residual_spans(haystack, concepts.residual_terms)
    if not residual_spans:
        return False
    for r_start, r_end in residual_spans:
        local = haystack[max(0, r_start - 8) : r_end + 9]
        if any(token in _BACKDROP_MARKERS for token in local):
            return True
        # Captions often say "promotional wall/backdrop featuring Masters' Union".
        if any(
            token in {"featuring", "reads", "reading", "says", "bearing", "displaying", "printed"}
            for token in local
        ) and any(
            token in _BACKDROP_MARKERS | {"wall", "stage", "booth", "display"}
            for token in local
        ):
            return True
    return False


def association_search_phrases(concepts: QueryConcepts) -> tuple[str, ...]:
    """Recall phrases that keep apparel/signage and residual intent together."""
    residual = " ".join(concepts.residual_terms)
    if not residual:
        return ()
    phrases: list[str] = []
    apparel = apparel_labels_in(concepts)
    for label in apparel:
        phrases.extend(
            (
                f"{residual} {label}",
                f"{label} with {residual}",
                f"{label} with {residual} logo",
                f"wearing {residual} {label}",
                f"matching {residual} {label}",
            )
        )
    if concepts.require_signage_brand:
        phrases.extend(
            (
                f"{residual} backdrop",
                f"backdrop with {residual}",
                f"banner with {residual}",
                f"promotional wall {residual}",
                f"background featuring {residual}",
            )
        )
    return tuple(dict.fromkeys(phrases))


def all_query_concepts_supported(
    concepts: QueryConcepts,
    *,
    structured_labels: Iterable[str] = (),
    evidence_texts: Iterable[str | None] = (),
    caption: str | None = None,
) -> bool:
    """Require every taxonomy and residual concept across available evidence."""
    labels = set(structured_labels)
    searchable_text = " ".join(
        part for part in [caption, *evidence_texts] if part
    )
    for label in concepts.taxonomy_labels:
        if label in labels:
            continue
        # Scene-brand queries accept backdrop evidence in place of a "sign" tag.
        if concepts.require_signage_brand and label in {"sign", "text", "logo"}:
            if residual_associated_with_signage(
                caption, concepts, evidence_texts=evidence_texts
            ):
                continue
        taxon = next((item for item in TAXONOMY if item.name == label), None)
        terms = taxon.terms if taxon is not None else (label,)
        if not any(text_supports_concept(searchable_text, term) for term in terms):
            # Backdrop vocabulary covers sign intent for scene-brand queries.
            if concepts.require_signage_brand and any(
                text_supports_concept(searchable_text, marker)
                for marker in _BACKDROP_MARKERS
            ):
                continue
            return False
    if not all(
        text_supports_concept(searchable_text, term)
        for term in concepts.residual_terms
    ):
        return False
    # Apparel + brand/modifier queries must attach the residual to the garment.
    if apparel_labels_in(concepts) and concepts.residual_terms:
        return residual_associated_with_apparel(
            caption,
            concepts,
            evidence_texts=evidence_texts,
        )
    if concepts.require_signage_brand:
        return residual_associated_with_signage(
            caption,
            concepts,
            evidence_texts=evidence_texts,
        )
    return True
