"""Versioned, deterministic object taxonomy and lexical classifier."""
from __future__ import annotations

import re
from dataclasses import dataclass

TAXONOMY_VERSION = "objects-v1"
OBJECT_MODEL_VERSION = f"{TAXONOMY_VERSION}-caption-gemini-embed"


@dataclass(frozen=True)
class Taxon:
    name: str
    category: str
    aliases: tuple[str, ...] = ()

    @property
    def terms(self) -> tuple[str, ...]:
        return (self.name, *self.aliases)


TAXONOMY: tuple[Taxon, ...] = (
    Taxon("t-shirt", "apparel", ("tshirt", "tee", "tee shirt", "t shirt")),
    Taxon("jersey", "apparel", ("kit", "sports shirt", "team shirt")),
    Taxon("uniform", "apparel", ("school uniform", "work uniform")),
    Taxon("shirt", "apparel"), Taxon("hoodie", "apparel"), Taxon("jacket", "apparel"),
    Taxon("dress", "apparel"), Taxon("suit", "apparel"), Taxon("hat", "apparel", ("cap",)),
    Taxon("shoe", "apparel", ("shoes", "sneaker", "sneakers")),
    Taxon("logo", "signage", ("brand mark", "emblem")),
    Taxon("sign", "signage", ("signage", "banner", "poster", "billboard")),
    Taxon("text", "signage", ("writing", "lettering")),
    Taxon("football", "sports_equipment", ("soccer ball",)),
    Taxon("basketball", "sports_equipment"), Taxon("cricket bat", "sports_equipment"),
    Taxon("tennis racket", "sports_equipment", ("tennis racquet",)),
    Taxon("dumbbell", "sports_equipment", ("dumbbells", "hand weight")),
    Taxon("barbell", "sports_equipment"),
    Taxon(
        "rowing machine",
        "sports_equipment",
        (
            "rowerg",
            "ergometer",
            "rower",
            "rowers",
            "row machine",
            "row machines",
            "rowing machines",
            "row erg",
            "row-erg",
            "indoor rowing",
            "rowing",
        ),
    ),
    Taxon("bicycle", "vehicle", ("bike", "cycle")), Taxon("motorcycle", "vehicle", ("motorbike",)),
    Taxon("car", "vehicle", ("automobile",)), Taxon("bus", "vehicle"), Taxon("truck", "vehicle"),
    Taxon("train", "vehicle"), Taxon("airplane", "vehicle", ("aeroplane", "plane",)),
    Taxon("boat", "vehicle", ("ship",)),
    Taxon("phone", "electronics", ("smartphone", "mobile phone", "cell phone")),
    Taxon("laptop", "electronics", ("notebook computer",)), Taxon("computer", "electronics", ("desktop",)),
    Taxon("tablet", "electronics"), Taxon("television", "electronics", ("tv", "monitor", "screen")),
    Taxon("camera", "electronics"), Taxon("microphone", "electronics", ("mic",)),
    Taxon("headphones", "electronics", ("headset", "earphones")),
    Taxon("chair", "furniture"), Taxon("table", "furniture", ("desk",)),
    Taxon("sofa", "furniture", ("couch",)), Taxon("bed", "furniture"), Taxon("shelf", "furniture", ("bookcase",)),
    Taxon("pizza", "food"), Taxon("burger", "food", ("hamburger",)), Taxon("sandwich", "food"),
    Taxon("cake", "food"), Taxon("fruit", "food"), Taxon("apple", "food"), Taxon("banana", "food"),
    Taxon("coffee", "food"), Taxon("bottle", "common"), Taxon("cup", "common", ("mug",)),
    Taxon("wine glass", "common", ("glass of wine",)), Taxon("book", "common"),
    Taxon("backpack", "common", ("rucksack", "school bag")), Taxon("bag", "common"),
    Taxon("umbrella", "common"), Taxon("clock", "common"), Taxon("watch", "common"),
    Taxon("whiteboard", "common"), Taxon("podium", "common", ("lectern",)),
    Taxon("trophy", "common", ("award cup",)), Taxon("certificate", "common"),
)

COLORS: tuple[str, ...] = (
    "black", "white", "red", "blue", "green", "yellow", "orange", "purple",
    "pink", "brown", "gray", "grey", "gold", "silver",
)

_BY_NAME = {taxon.name: taxon for taxon in TAXONOMY}
_ALIAS_TO_NAME = {
    re.sub(r"[\s_-]+", " ", term.casefold()).strip(): taxon.name
    for taxon in TAXONOMY
    for term in taxon.terms
}


def canonicalize_object(value: str) -> str | None:
    """Return a canonical label for an exact object/synonym query."""
    key = re.sub(r"[\s_-]+", " ", (value or "").casefold()).strip()
    if key in _ALIAS_TO_NAME:
        return _ALIAS_TO_NAME[key]
    if key.endswith("s") and key[:-1] in _ALIAS_TO_NAME:
        return _ALIAS_TO_NAME[key[:-1]]
    return key if key in COLORS else None


def object_query_labels(query: str) -> tuple[str, ...]:
    """Find canonical object tags using longest non-overlapping alias spans."""
    from app.objects.query_concepts import parse_query_concepts

    return parse_query_concepts(query).taxonomy_labels


def taxon_for(name: str) -> Taxon | None:
    return _BY_NAME.get(name)


def classify_text(text: str | None) -> list[dict[str, object]]:
    """Extract high-confidence tags using whole phrase matches only."""
    haystack = re.sub(r"[\s_-]+", " ", (text or "").casefold())
    ski_erg_context = bool(
        re.search(r"\bski[\s-]?ergs?\b|\bskiergs?\b|\bski\s+ergometers?\b", haystack)
    )
    labels: list[dict[str, object]] = []
    for taxon in TAXONOMY:
        # "ergometer" is shared with ski ergs — never tag pure ski-erg captions as rowing.
        if (
            taxon.name == "rowing machine"
            and ski_erg_context
            and not re.search(r"\b(?:rowing|rowers?|rowerg|row machines?)\b", haystack)
        ):
            continue
        hits = [
            term for term in taxon.terms
            if re.search(rf"(?<!\w){re.escape(term.casefold())}(?:s)?(?!\w)", haystack)
        ]
        if hits:
            labels.append({
                "canonical_label": taxon.name,
                "category": taxon.category,
                "confidence": min(0.99, 0.90 + 0.02 * (len(hits) - 1)),
                "evidence_source": "caption",
                "evidence_text": hits[0],
                "hit_count": len(hits),
            })
    for color in COLORS:
        if re.search(rf"(?<!\w){re.escape(color)}(?!\w)", haystack):
            canonical = "gray" if color == "grey" else color
            if not any(item["canonical_label"] == canonical for item in labels):
                labels.append({
                    "canonical_label": canonical,
                    "category": "color",
                    "confidence": 0.92,
                    "evidence_source": "caption",
                    "evidence_text": color,
                    "hit_count": 1,
                })
    return labels
