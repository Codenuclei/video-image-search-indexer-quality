#!/usr/bin/env python3
"""Build a ~500-query CSV dataset from indexed image captions.

Designed to run in the backend environment (Railway or local with Qdrant access):

    cd /app && PYTHONPATH=/app python scripts/build_caption_query_dataset.py \\
        --out /tmp/caption_queries_500.csv --target 500
"""
from __future__ import annotations

import argparse
import csv
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

# Allow `python scripts/...` from repo root or /app.
ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
if str(BACKEND) not in sys.path and (BACKEND / "app").is_dir():
    sys.path.insert(0, str(BACKEND))
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if Path("/app/app").is_dir() and "/app" not in sys.path:
    sys.path.insert(0, "/app")


STOP = frozenset(
    """
    a an the and or of to in on at for from with by as is are was were be been being
    this that these those it its they them their he she his her we our you your
    man men woman women person people group someone somebody young older adult adults
    stands standing stand poses pose posing sits sitting sit holding holds hold
    wearing wears wear worn while during after before over under near beside front
    behind across against around inside outside outdoor outdoors indoor indoors
    large small tall short long brightly lit visible looking toward towards camera
    photo image picture photos images featuring featuring reads reading text reads
    left right top bottom side middle center next also both each other another
    using used use makes making made give gives giving thumbs up down
    """.split()
)

BRAND_PATTERNS = (
    (re.compile(r"masters['’]?\s*union|mastersunion", re.I), "mastersunion"),
    (re.compile(r"\bhyrox\b", re.I), "hyrox"),
    (re.compile(r"\bnivia\b", re.I), "nivia"),
    (re.compile(r"\bquestify\b", re.I), "questify"),
    (re.compile(r"\bmu\b"), "mu"),
)

APPAREL = (
    ("t-shirt", re.compile(r"\bt-?shirts?\b|\btees?\b|\btshirts?\b", re.I)),
    ("shirt", re.compile(r"\bshirts?\b", re.I)),
    ("hoodie", re.compile(r"\bhoodies?\b", re.I)),
    ("jacket", re.compile(r"\bjackets?\b", re.I)),
    ("hat", re.compile(r"\bhats?\b|\bcaps?\b|\bbaseball caps?\b", re.I)),
    ("jersey", re.compile(r"\bjerseys?\b", re.I)),
    ("suit", re.compile(r"\bsuits?\b", re.I)),
    ("dress", re.compile(r"\bdresses?\b", re.I)),
    ("shoe", re.compile(r"\bshoes?\b|\bsneakers?\b", re.I)),
    ("shorts", re.compile(r"\bshorts?\b", re.I)),
    ("pants", re.compile(r"\bpants?\b|\btrousers?\b", re.I)),
    ("uniform", re.compile(r"\buniforms?\b", re.I)),
    ("gown", re.compile(r"\bgowns?\b", re.I)),
    ("vest", re.compile(r"\bvests?\b", re.I)),
    ("scarf", re.compile(r"\bscar(?:f|ves)\b", re.I)),
    ("gloves", re.compile(r"\bgloves?\b", re.I)),
)

OBJECTS = (
    ("phone", re.compile(r"\bphones?\b|\bsmartphones?\b", re.I)),
    ("laptop", re.compile(r"\blaptops?\b", re.I)),
    ("microphone", re.compile(r"\bmicrophones?\b|\bmics?\b", re.I)),
    ("trophy", re.compile(r"\btroph(?:y|ies)\b", re.I)),
    ("award", re.compile(r"\bawards?\b|\bmedals?\b", re.I)),
    ("bag", re.compile(r"\bbags?\b|\bbackpacks?\b", re.I)),
    ("bottle", re.compile(r"\bbottles?\b", re.I)),
    ("dumbbell", re.compile(r"\bdumbbells?\b|\bweights?\b", re.I)),
    ("bicycle", re.compile(r"\bbicycles?\b|\bbikes?\b", re.I)),
    ("certificate", re.compile(r"\bcertificates?\b", re.I)),
    ("whiteboard", re.compile(r"\bwhiteboards?\b", re.I)),
    ("podium", re.compile(r"\bpodiums?\b|\blecterns?\b", re.I)),
    ("flag", re.compile(r"\bflags?\b", re.I)),
    ("ball", re.compile(r"\bballs?\b|\bbasketballs?\b|\bfootballs?\b", re.I)),
    ("racket", re.compile(r"\brackets?\b|\bbats?\b", re.I)),
    ("camera", re.compile(r"\bcameras?\b", re.I)),
    ("tablet", re.compile(r"\btablets?\b|\bipads?\b", re.I)),
    ("chair", re.compile(r"\bchairs?\b|\barmchairs?\b", re.I)),
    ("table", re.compile(r"\btables?\b|\bdesks?\b", re.I)),
    ("box", re.compile(r"\bbox(?:es)?\b|\bcardboard\b", re.I)),
    ("sign", re.compile(r"\bsigns?\b|\bplaques?\b", re.I)),
    ("banner", re.compile(r"\bbanners?\b", re.I)),
    ("backdrop", re.compile(r"\bbackdrops?\b", re.I)),
    ("stage", re.compile(r"\bstages?\b", re.I)),
    ("flower", re.compile(r"\bflowers?\b|\bvases?\b", re.I)),
    ("card", re.compile(r"\bcards?\b", re.I)),
    ("document", re.compile(r"\bdocuments?\b|\bpapers?\b", re.I)),
    ("glasses", re.compile(r"\bglasses?\b|\bspectacles?\b|\bsunglasses?\b", re.I)),
    ("watch", re.compile(r"\bwatches?\b", re.I)),
    ("helmet", re.compile(r"\bhelmets?\b", re.I)),
    ("skateboard", re.compile(r"\bskateboards?\b", re.I)),
    ("scooter", re.compile(r"\bscooters?\b", re.I)),
    ("car", re.compile(r"\bcars?\b|\bvehicles?\b", re.I)),
    ("bus", re.compile(r"\bbuses?\b", re.I)),
    ("train", re.compile(r"\btrains?\b", re.I)),
    ("umbrella", re.compile(r"\bumbrellas?\b", re.I)),
    ("mirror", re.compile(r"\bmirrors?\b", re.I)),
    ("screen", re.compile(r"\bscreens?\b|\bmonitors?\b|\bdisplays?\b", re.I)),
    ("keyboard", re.compile(r"\bkeyboards?\b", re.I)),
    ("mouse", re.compile(r"\bmice\b|\bmouses?\b", re.I)),
    ("pen", re.compile(r"\bpencils?\b|\bpenns?\b|\bmarkers?\b", re.I)),
    ("book", re.compile(r"\bbooks?\b|\bnotebooks?\b", re.I)),
    ("map", re.compile(r"\bmaps?\b", re.I)),
    ("clock", re.compile(r"\bclocks?\b", re.I)),
    ("ladder", re.compile(r"\bladders?\b", re.I)),
    ("rope", re.compile(r"\bropes?\b", re.I)),
    ("net", re.compile(r"\bnets?\b", re.I)),
    ("cone", re.compile(r"\bcones?\b", re.I)),
    ("mat", re.compile(r"\bmats?\b|\byoga mats?\b", re.I)),
    ("treadmill", re.compile(r"\btreadmills?\b", re.I)),
    ("rower", re.compile(r"\browers?\b|\bergometers?\b", re.I)),
)

COLORS = (
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
    "neon",
    "maroon",
    "navy",
    "beige",
    "gold",
    "silver",
    "cream",
    "teal",
)

ACTIONS = (
    ("graduating", re.compile(r"\bgraduat(?:e|es|ion|ing)\b|\bconvocation\b|\bacademic gown", re.I)),
    ("exercising", re.compile(r"\bexercis(?:e|ing)\b|\bworkout\b|\bfitness\b|\bgym\b", re.I)),
    ("rowing", re.compile(r"\browing\b|\bergometer\b", re.I)),
    ("speaking on stage", re.compile(r"\bon (?:a |the )?stage\b|\bpanel discussion\b", re.I)),
    ("jumping", re.compile(r"\bjumps?\b|\bjumping\b", re.I)),
    ("celebrating", re.compile(r"\bcelebrat(?:e|es|ing|ion)\b|\bcheer(?:s|ing)?\b", re.I)),
    ("posing for group photo", re.compile(r"\bgroup (?:photo|photograph|pose)\b|\bposing together\b", re.I)),
    ("running", re.compile(r"\brunners?\b|\brunning\b|\bjogg(?:er|ing)\b", re.I)),
    ("lifting weights", re.compile(r"\blifting\b|\bdeadlift\b|\bsquat(?:s|ting)?\b", re.I)),
    ("clapping", re.compile(r"\bclapp(?:ing|s)\b|\bapplause\b", re.I)),
    ("dancing", re.compile(r"\bdanc(?:e|es|ing)\b", re.I)),
    ("eating", re.compile(r"\beating\b|\bfood\b|\bmeal\b", re.I)),
    ("presenting", re.compile(r"\bpresent(?:ing|ation)\b|\bspeaking\b|\bkeynote\b", re.I)),
    ("networking", re.compile(r"\bnetwork(?:ing)?\b|\bmixing\b", re.I)),
    ("stretching", re.compile(r"\bstretch(?:ing|es)?\b|\bwarm[- ]?up\b", re.I)),
    ("racing", re.compile(r"\brac(?:e|es|ing)\b|\bcompetition\b", re.I)),
)

SCENES = (
    ("backdrop with text", re.compile(r"\bbackdrop\b|\bbanner\b|\bsignage\b|\bbillboard\b", re.I)),
    ("indoor event", re.compile(r"\bindoor (?:event|venue|track)\b|\bevent space\b", re.I)),
    ("office lobby", re.compile(r"\blobby\b|\breception\b", re.I)),
    ("office", re.compile(r"\boffice\b", re.I)),
    ("factory", re.compile(r"\bfactory\b|\bmanufacturing\b|\bplant\b|\bwarehouse\b", re.I)),
    ("classroom", re.compile(r"\bclassroom\b|\blecture\b", re.I)),
    ("conference room", re.compile(r"\bconference\b|\bmeeting room\b", re.I)),
    ("basketball court", re.compile(r"\bbasketball court\b|\bcourt\b", re.I)),
    ("outdoor track", re.compile(r"\btrack\b|\bstadium\b|\barena\b", re.I)),
    ("stage", re.compile(r"\bon (?:a |the )?stage\b", re.I)),
    ("parking lot", re.compile(r"\bparking\b", re.I)),
    ("rooftop", re.compile(r"\brooftop\b|\broof\b", re.I)),
    ("cafe", re.compile(r"\bcafe\b|\bcafé\b|\bcoffee shop\b", re.I)),
    ("gym", re.compile(r"\bgym\b|\bfitness center\b", re.I)),
)


def _norm_query(q: str) -> str:
    return re.sub(r"\s+", " ", q.strip().lower())


def _brands(caption: str) -> list[str]:
    out: list[str] = []
    for rx, name in BRAND_PATTERNS:
        if rx.search(caption) and name not in out:
            out.append(name)
    return out


def _all_matches(patterns: tuple[tuple[str, re.Pattern], ...], caption: str) -> list[str]:
    out: list[str] = []
    for name, rx in patterns:
        if rx.search(caption) and name not in out:
            out.append(name)
    return out


def _colors(caption: str) -> list[str]:
    low = caption.lower()
    return [c for c in COLORS if re.search(rf"\b{re.escape(c)}\b", low)]


def _quoted_phrases(caption: str) -> list[str]:
    phrases = []
    for m in re.finditer(r"[\"“”']([^\"“”']{2,48})[\"“”']", caption):
        phrase = m.group(1).strip()
        if 2 <= len(phrase.split()) <= 8 and phrase.lower() not in STOP:
            phrases.append(phrase)
    return phrases[:5]


def _content_tokens(caption: str) -> list[str]:
    tokens = []
    for w in re.findall(r"[A-Za-z][A-Za-z'-]{2,}", caption):
        low = w.lower()
        if low in STOP or w.isupper() and len(w) <= 3:
            continue
        tokens.append(low)
    return tokens


def generate_from_caption(caption: str) -> list[tuple[str, str, str]]:
    """Return list of (query, query_type, evidence_snippet)."""
    cap = (caption or "").strip()
    if len(cap) < 40:
        return []
    rows: list[tuple[str, str, str]] = []
    snippet = re.sub(r"\s+", " ", cap)[:160]
    brands = _brands(cap)
    apparels = _all_matches(APPAREL, cap)
    objects = _all_matches(OBJECTS, cap)
    actions = _all_matches(ACTIONS, cap)
    scenes = _all_matches(SCENES, cap)
    colors = _colors(cap)
    quotes = _quoted_phrases(cap)
    tokens = _content_tokens(cap)

    for apparel in apparels:
        rows.append((apparel, "object", snippet))
        for color in colors[:2]:
            rows.append((f"{color} {apparel}", "color_object", snippet))
        for brand in brands:
            rows.append((f"{apparel} with text {brand}", "apparel_brand", snippet))
            rows.append((f"{brand} {apparel}", "brand_apparel", snippet))
            rows.append((f"people wearing {brand} {apparel}", "apparel_brand", snippet))
            rows.append((f"matching {brand} {apparel}", "apparel_brand", snippet))
            for color in colors[:1]:
                rows.append((f"{color} {apparel} with text {brand}", "color_apparel_brand", snippet))

    for scene in scenes:
        rows.append((scene, "scene", snippet))
        for brand in brands:
            rows.append((f"background with text {brand}", "signage_brand", snippet))
            rows.append((f"{scene} {brand}", "scene_brand", snippet))
            rows.append((f"{brand} backdrop", "signage_brand", snippet))

    if brands and not scenes:
        for brand in brands:
            rows.append((f"background with text {brand}", "signage_brand", snippet))
            rows.append((brand, "brand", snippet))

    for obj in objects:
        rows.append((obj, "object", snippet))
        for color in colors[:2]:
            rows.append((f"{color} {obj}", "color_object", snippet))
        for apparel in apparels[:2]:
            rows.append((f"{apparel} with {obj}", "multi_object", snippet))
        for brand in brands[:1]:
            rows.append((f"{obj} with text {brand}", "object_brand", snippet))

    for action in actions:
        rows.append((action, "action", snippet))
        for brand in brands:
            rows.append((f"{action} {brand}", "action_brand", snippet))

    for phrase in quotes:
        compact = re.sub(r"[^a-z0-9]+", " ", phrase.lower()).strip()
        if not compact or compact in {"masters union", "hyrox", "masters union hyrox"}:
            continue
        rows.append((phrase.lower(), "quoted_text", snippet))
        for apparel in apparels[:1]:
            rows.append((f"{apparel} with text {compact}", "apparel_quoted", snippet))

    # Caption n-grams for naturalistic free-text queries.
    for i in range(len(tokens) - 1):
        bigram = f"{tokens[i]} {tokens[i + 1]}"
        if 5 <= len(bigram) <= 36:
            rows.append((bigram, "caption_bigram", snippet))
    for i in range(len(tokens) - 2):
        trigram = f"{tokens[i]} {tokens[i + 1]} {tokens[i + 2]}"
        if 8 <= len(trigram) <= 48:
            rows.append((trigram, "caption_trigram", snippet))

    # Person + object / apparel templates.
    for apparel in apparels[:2]:
        rows.append((f"person wearing {apparel}", "person_object", snippet))
    for obj in objects[:2]:
        rows.append((f"person holding {obj}", "person_object", snippet))

    return rows


def scroll_captions(limit_points: int | None = None) -> list[str]:
    from app.config import get_settings
    from app.qdrant.client import make_qdrant_client
    from app.qdrant.image_captions import is_valid_caption

    settings = get_settings()
    client = make_qdrant_client(settings.qdrant_url, timeout=120)
    collection = settings.qdrant_image_captions_collection
    offset = None
    captions: list[str] = []
    while True:
        points, offset = client.scroll(
            collection_name=collection,
            limit=500,
            offset=offset,
            with_payload=True,
            with_vectors=False,
        )
        for point in points:
            payload = point.payload or {}
            caption = str(payload.get("caption") or "").strip()
            if is_valid_caption(caption):
                captions.append(caption)
                if limit_points and len(captions) >= limit_points:
                    return captions
        if offset is None:
            break
    return captions


def select_queries(
    captions: list[str],
    *,
    target: int,
) -> list[dict[str, str | int]]:
    # Soft per-type caps for diversity; leftovers + n-grams fill to target.
    bucket_caps = {
        "apparel_brand": 100,
        "brand_apparel": 50,
        "color_apparel_brand": 50,
        "signage_brand": 60,
        "scene_brand": 40,
        "object": 80,
        "color_object": 60,
        "multi_object": 40,
        "object_brand": 30,
        "action": 50,
        "action_brand": 30,
        "scene": 40,
        "brand": 20,
        "quoted_text": 40,
        "apparel_quoted": 30,
        "caption_bigram": 120,
        "caption_trigram": 100,
        "person_object": 40,
    }
    buckets: dict[str, list[tuple[str, str]]] = defaultdict(list)
    overflow: list[tuple[str, str, str]] = []
    seen_q: set[str] = set()

    for cap in captions:
        for query, qtype, snippet in generate_from_caption(cap):
            nq = _norm_query(query)
            if len(nq) < 3 or nq in seen_q:
                continue
            if qtype not in bucket_caps:
                continue
            seen_q.add(nq)
            if len(buckets[qtype]) < bucket_caps[qtype]:
                buckets[qtype].append((query.strip(), snippet))
            else:
                overflow.append((query.strip(), qtype, snippet))

    order = list(bucket_caps.keys())
    selected: list[dict[str, str | int]] = []
    selected_norms: set[str] = set()

    def _add(query: str, qtype: str, snippet: str) -> bool:
        if len(selected) >= target:
            return False
        nq = _norm_query(query)
        if nq in selected_norms or len(nq) < 3:
            return False
        selected_norms.add(nq)
        selected.append(
            {
                "query_id": len(selected) + 1,
                "query": query,
                "query_type": qtype,
                "source_caption_snippet": snippet,
            }
        )
        return True

    for qtype in order:
        for query, snippet in buckets[qtype]:
            if not _add(query, qtype, snippet):
                if len(selected) >= target:
                    break
        if len(selected) >= target:
            break

    if len(selected) < target:
        for query, qtype, snippet in overflow:
            if not _add(query, qtype, snippet):
                if len(selected) >= target:
                    break

    # Synthesize remaining apparel×brand / color×object combos from corpus stats.
    brand_counts: Counter[str] = Counter()
    apparel_brand_pairs: set[tuple[str, str]] = set()
    color_apparel_pairs: set[tuple[str, str]] = set()
    object_hits: Counter[str] = Counter()
    for cap in captions:
        brands = _brands(cap)
        apparels = _all_matches(APPAREL, cap)
        objects = _all_matches(OBJECTS, cap)
        colors = _colors(cap)
        for b in brands:
            brand_counts[b] += 1
            for a in apparels:
                apparel_brand_pairs.add((a, b))
        for a in apparels:
            for c in colors:
                color_apparel_pairs.add((c, a))
        for o in objects:
            object_hits[o] += 1

    synth_templates = [
        ("{apparel} with text {brand}", "apparel_brand"),
        ("{brand} {apparel}", "brand_apparel"),
        ("people wearing {brand} {apparel}", "apparel_brand"),
        ("background with text {brand}", "signage_brand"),
        ("{brand} backdrop", "signage_brand"),
        ("matching {brand} {apparel}", "apparel_brand"),
        ("{color} {apparel} with text {brand}", "color_apparel_brand"),
        ("{brand} logo on {apparel}", "apparel_brand"),
        ("{apparel} printed {brand}", "apparel_brand"),
    ]
    for apparel, brand in sorted(apparel_brand_pairs):
        for tmpl, qtype in synth_templates:
            if len(selected) >= target:
                break
            try:
                query = tmpl.format(
                    apparel=apparel,
                    brand=brand,
                    color=next(iter(c for c, a in color_apparel_pairs if a == apparel), "black"),
                )
            except Exception:
                continue
            _add(
                query,
                qtype,
                f"synthesized from caption co-occurrence ({brand_counts[brand]} caps)",
            )

    for color, apparel in sorted(color_apparel_pairs):
        if len(selected) >= target:
            break
        _add(f"{color} {apparel}", "color_object", "synthesized color+apparel co-occurrence")

    for obj_name, count in object_hits.most_common():
        if len(selected) >= target:
            break
        _add(obj_name, "object", f"frequent object in captions ({count})")
        for brand, _ in brand_counts.most_common(3):
            _add(f"{obj_name} with text {brand}", "object_brand", "synthesized object+brand")

    # Last resort: ranked caption unigrams that look like searchable nouns.
    if len(selected) < target:
        unigram_counts: Counter[str] = Counter()
        for cap in captions:
            for tok in _content_tokens(cap):
                if 4 <= len(tok) <= 18 and tok.isalpha():
                    unigram_counts[tok] += 1
        for tok, count in unigram_counts.most_common(800):
            if len(selected) >= target:
                break
            if count < 3:
                break
            _add(tok, "caption_unigram", f"frequent caption noun ({count})")

    for i, row in enumerate(selected, start=1):
        row["query_id"] = i
    return selected[:target]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--target", type=int, default=500)
    parser.add_argument("--max-captions", type=int, default=0, help="0 = all captions")
    args = parser.parse_args()

    limit = args.max_captions if args.max_captions > 0 else None
    print(f"Scrolling captions (limit={limit or 'all'})...", flush=True)
    captions = scroll_captions(limit_points=limit)
    print(f"Loaded {len(captions)} captions", flush=True)
    rows = select_queries(captions, target=args.target)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(
            fh,
            fieldnames=["query_id", "query", "query_type", "source_caption_snippet"],
        )
        writer.writeheader()
        writer.writerows(rows)
    type_counts = Counter(str(r["query_type"]) for r in rows)
    print(f"Wrote {len(rows)} queries -> {args.out}", flush=True)
    for qtype, count in type_counts.most_common():
        print(f"  {qtype}: {count}", flush=True)


if __name__ == "__main__":
    main()
