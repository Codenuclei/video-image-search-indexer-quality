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

    @property
    def is_conjunctive_object_query(self) -> bool:
        return bool(self.taxonomy_labels) and (
            len(self.taxonomy_labels) + len(self.residual_terms) > 1
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
    for token, used in zip(tokens, occupied):
        if not used and token not in _CONTENT_STOPWORDS and token not in residual:
            residual.append(token)
    return QueryConcepts(tuple(labels), tuple(residual))


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
        taxon = next((item for item in TAXONOMY if item.name == label), None)
        terms = taxon.terms if taxon is not None else (label,)
        if not any(text_supports_concept(searchable_text, term) for term in terms):
            return False
    return all(
        text_supports_concept(searchable_text, term)
        for term in concepts.residual_terms
    )
