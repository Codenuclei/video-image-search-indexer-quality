#!/usr/bin/env python3
"""Build datasets/startups/catalog.json from PDF + web scratch files.

Re-runnable: overwrites catalog.json and assets/<slug>/ on every run.

Default OUTPUT_DIR is backend/datasets/startups (inside the Docker build context).
"""

from __future__ import annotations

import io
import json
import os
import re
import shutil
import sys
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import httpx
import pymupdf
from PIL import Image

REPO_ROOT = Path(__file__).resolve().parents[2]
BACKEND_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PDF = REPO_ROOT / "Entrepreneurship_Report.pdf"
DEFAULT_PDF_SCRATCH = Path("/tmp/entrepreneurship_report_startups.json")
DEFAULT_WEB_SCRATCH = Path("/tmp/mu_startups_web.json")
# Lives under backend/ so Railway `cd backend && railway up` includes the catalog.
OUT_DIR = BACKEND_ROOT / "datasets" / "startups"
ASSETS_DIR = OUT_DIR / "assets"
CATALOG_PATH = OUT_DIR / "catalog.json"

SKIP_PAGES = {8, 9, 72, 73, 74}
DECORATIVE_PIXEL_SIZES = {(1499, 543)}
MAX_COVER_IMAGES = 3
OPENROUTER_MODELS = ("x-ai/grok-4-fast", "x-ai/grok-4.1-fast")
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

# Canonical PDF display name -> known source-name variants (web + spelling).
HARDCODED_ALIASES: dict[str, list[str]] = {
    "EIGHT": ["Eight.Network", "Eight Network", "eight.network", "EIGHT"],
    "Mom's Mixes": ["Moms", "Mom's Mixes", "Moms Mixes", "Mom’s Mixes"],
    "Bana Roma": ["Banaroma", "Bana Roma", "BanaRoma"],
    "Truebrands": ["TrueBrandsIndia", "Truebrands", "True Brands", "True Brands India"],
    "Bambaii Foods": ["Bambaii", "Bambaii Foods"],
    "Kazé": ["Kaze", "Kazé", "Kaze Drinks"],
    "Beyond Veda": ["BeyondVeda", "Beyond Veda"],
    "Flourish Foods": ["FlourishFoods", "Flourish Foods"],
    "Woody's Pizzeria": ["WoodysPizzeria", "Woody's Pizzeria", "Woody’s Pizzeria", "Woodys Pizzeria"],
    "Blue Brew": ["BlueBrew", "Blue Brew"],
    "Witty Adverts": ["WittyAdverts", "Witty Adverts"],
    "Spawn Right": ["SpawnRight", "Spawn Right"],
    "Lexi's": ["Lexi's", "Lexi’s", "Lexi's Gourmet Sandwiches", "Lexi’s Gourmet Sandwiches", "Lexis"],
    "Saaha India": ["Saaha", "Saaha India"],
}

INCOMPLETE_FOUNDER_RE = re.compile(r"not stated|last name", re.I)


def load_dotenv_files() -> None:
    try:
        from dotenv import load_dotenv
    except ImportError:
        load_dotenv = None
    repo_env = REPO_ROOT / ".env"
    backend_env = BACKEND_ROOT / ".env"
    if load_dotenv is not None:
        if repo_env.is_file():
            load_dotenv(repo_env, override=True)
        if backend_env.is_file():
            load_dotenv(backend_env, override=False)
    else:
        for path in (repo_env, backend_env):
            if not path.is_file():
                continue
            for line in path.read_text(encoding="utf-8").splitlines():
                raw = line.strip()
                if not raw or raw.startswith("#") or "=" not in raw:
                    continue
                key, value = raw.split("=", 1)
                os.environ.setdefault(key.strip(), value.strip().strip("'").strip('"'))
    key = (os.environ.get("OPENROUTER_API_KEY") or "").strip().strip("'").strip('"')
    if key:
        os.environ["OPENROUTER_API_KEY"] = key


def fold_accents(text: str) -> str:
    normalized = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in normalized if not unicodedata.combining(ch))


def normalize_key(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", fold_accents(name).lower())


def slugify(name: str) -> str:
    text = fold_accents(name).lower()
    text = re.sub(r"['’`´]", "", text)
    text = re.sub(r"[^a-z0-9]+", "-", text)
    return text.strip("-")


def pretty_alias(name: str) -> str:
    text = fold_accents(name).lower().replace("’", "'")
    text = re.sub(r"[^a-z0-9.'&+\- ]+", " ", text)
    return re.sub(r"\s+", " ", text).strip(" .-")


def build_alias_index() -> dict[str, str]:
    index: dict[str, str] = {}
    for canonical, variants in HARDCODED_ALIASES.items():
        for variant in [*variants, canonical]:
            index[normalize_key(variant)] = canonical
    return index


ALIAS_INDEX = build_alias_index()


def resolve_canonical(name: str) -> str:
    key = normalize_key(name)
    if key in ALIAS_INDEX:
        return ALIAS_INDEX[key]
    return name


def heuristic_aliases(canonical: str, extra_names: list[str]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []

    def add(value: str) -> None:
        alias = pretty_alias(value)
        if not alias or alias in seen:
            return
        seen.add(alias)
        ordered.append(alias)
        compact = re.sub(r"[^a-z0-9]", "", alias)
        if compact and compact not in seen:
            seen.add(compact)
            ordered.append(compact)
        dashed = slugify(alias)
        if dashed and dashed not in seen:
            seen.add(dashed)
            ordered.append(dashed)

    add(canonical)
    for name in extra_names:
        add(name)
    for variant in HARDCODED_ALIASES.get(canonical, []):
        add(variant)
    return ordered


def grok_aliases(canonical_names: list[str], known: dict[str, list[str]]) -> dict[str, list[str]]:
    api_key = (os.environ.get("OPENROUTER_API_KEY") or "").strip()
    if not api_key:
        print("OpenRouter key missing; using heuristic aliases only.")
        return {}

    payload_names = [
        {"name": name, "known_aliases": known.get(name, [])} for name in canonical_names
    ]
    prompt = (
        "You generate search aliases for startup brand names.\n"
        "Return ONLY a JSON object mapping each canonical name to an array of short aliases.\n"
        "Include lowercase forms, missing punctuation/spaces, common web slugs, "
        "and obvious brand shortenings. No sentences. No extra keys.\n\n"
        f"{json.dumps(payload_names, ensure_ascii=False)}"
    )
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://github.com/drivefaceindexer",
        "X-Title": "DriveFaceIndexer startup catalog",
    }
    last_error = ""
    for model in OPENROUTER_MODELS:
        body = {
            "model": model,
            "messages": [
                {"role": "system", "content": "Reply with a single JSON object and nothing else."},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.1,
            "response_format": {"type": "json_object"},
        }
        try:
            with httpx.Client(timeout=45.0) as client:
                response = client.post(OPENROUTER_URL, headers=headers, json=body)
                if response.status_code >= 400:
                    last_error = f"{model}: HTTP {response.status_code} {response.text[:180]}"
                    continue
            data = response.json()
            content = data["choices"][0]["message"]["content"]
            parsed = json.loads(content)
            if isinstance(parsed, dict):
                print(f"OpenRouter aliases from {model}.")
                return {
                    key: [str(item) for item in value]
                    for key, value in parsed.items()
                    if isinstance(value, list)
                }
            last_error = f"{model}: JSON was not an object"
        except Exception as exc:
            last_error = f"{model}: {exc}"
            continue
    print(f"OpenRouter aliases failed ({last_error}); using heuristic aliases.")
    return {}


def founder_incomplete(name: str) -> bool:
    return bool(INCOMPLETE_FOUNDER_RE.search(name))


def clean_founder_name(name: str) -> str:
    cleaned = re.sub(r"\s*\([^)]*\)\s*", " ", name)
    return re.sub(r"\s+", " ", cleaned).strip(" ,;")


def first_name_key(name: str) -> str:
    token = clean_founder_name(name).split()
    return fold_accents(token[0]).lower() if token else ""


def merge_founders(pdf_founders: list[str], web_founders: list[str]) -> list[dict[str, str]]:
    pdf_founders = [n for n in pdf_founders if str(n).strip()]
    web_founders = [n for n in web_founders if str(n).strip()]
    if not pdf_founders:
        return [{"name": name} for name in web_founders]
    if any(founder_incomplete(name) for name in pdf_founders) and web_founders:
        if all(founder_incomplete(name) for name in pdf_founders):
            return [{"name": name} for name in web_founders]
        web_by_first = {first_name_key(name): name for name in web_founders}
        merged: list[str] = []
        for name in pdf_founders:
            if founder_incomplete(name):
                replacement = web_by_first.get(first_name_key(name))
                merged.append(replacement or clean_founder_name(name))
            else:
                merged.append(name)
        return [{"name": name} for name in merged if name]
    return [{"name": name} for name in pdf_founders]


def candidate_founders(pdf_row: dict | None) -> list[dict[str, str]]:
    if not pdf_row:
        return []
    names = pdf_row.get("additional_founders_mentioned") or []
    return [{"name": name} for name in names if str(name).strip()]


def ext_from_url(url: str, default: str = ".png") -> str:
    suffix = Path(urlparse(url).path).suffix.lower()
    if suffix in {".png", ".jpg", ".jpeg", ".webp", ".gif", ".avif"}:
        return suffix
    return default


def ext_from_content_type(content_type: str | None, fallback: str) -> str:
    if not content_type:
        return fallback
    mapping = {
        "image/png": ".png",
        "image/jpeg": ".jpg",
        "image/jpg": ".jpg",
        "image/webp": ".webp",
        "image/gif": ".gif",
        "image/avif": ".avif",
    }
    return mapping.get(content_type.split(";")[0].strip().lower(), fallback)


def download_file(client: httpx.Client, url: str, dest: Path, failures: list[str], label: str) -> str | None:
    try:
        response = client.get(url)
        response.raise_for_status()
    except Exception as exc:
        failures.append(f"{label}: download failed ({exc})")
        return None
    suffix = ext_from_content_type(response.headers.get("content-type"), ext_from_url(url))
    dest = dest.with_suffix(suffix)
    dest.write_bytes(response.content)
    if dest.stat().st_size < 32:
        dest.unlink(missing_ok=True)
        failures.append(f"{label}: empty/tiny file from {url}")
        return None
    return dest.name


def page_images(page: pymupdf.Page) -> list[dict]:
    infos = []
    for info in page.get_image_info(xrefs=True):
        xref = int(info.get("xref") or 0)
        if xref <= 0:
            continue
        bbox = info.get("bbox") or (0, 0, 0, 0)
        width = int(info.get("width") or 0)
        height = int(info.get("height") or 0)
        disp_w = float(bbox[2] - bbox[0])
        disp_h = float(bbox[3] - bbox[1])
        infos.append(
            {
                "xref": xref,
                "width": width,
                "height": height,
                "bbox": bbox,
                "disp_w": disp_w,
                "disp_h": disp_h,
                "disp_area": abs(disp_w * disp_h),
                "y0": float(bbox[1]),
                "pixel_area": width * height,
            }
        )
    return infos


def xref_reuse_counts(doc: pymupdf.Document) -> dict[int, int]:
    counts: dict[int, int] = {}
    for page in doc:
        seen: set[int] = set()
        for img in page.get_images(full=True):
            xref = int(img[0])
            if xref in seen:
                continue
            seen.add(xref)
            counts[xref] = counts.get(xref, 0) + 1
    return counts


def is_junk_image(img: dict, reuse: dict[int, int]) -> bool:
    if (img["width"], img["height"]) in DECORATIVE_PIXEL_SIZES:
        return True
    if reuse.get(img["xref"], 0) >= 3:
        return True
    if min(img["disp_w"], img["disp_h"]) < 18:
        return True
    if img["pixel_area"] < 2500:
        return True
    return False


def is_wordmark(img: dict) -> bool:
    if img["y0"] > 110:
        return False
    if not (40 <= img["disp_w"] <= 360 and 18 <= img["disp_h"] <= 210):
        return False
    if img["disp_area"] > 45_000:
        return False
    ratio = img["disp_w"] / max(img["disp_h"], 1)
    return 0.6 <= ratio <= 8.0


def pick_cover_images(images: list[dict], reuse: dict[int, int]) -> list[int]:
    usable = [img for img in images if not is_junk_image(img, reuse)]
    wordmarks = sorted(
        [img for img in usable if is_wordmark(img)],
        key=lambda img: (img["y0"], -img["disp_area"]),
    )
    chosen: list[int] = []
    for img in wordmarks[:1]:
        chosen.append(img["xref"])
    remaining = [img for img in usable if img["xref"] not in chosen]
    remaining.sort(key=lambda img: (-img["disp_area"], img["y0"]))
    for img in remaining:
        if len(chosen) >= MAX_COVER_IMAGES:
            break
        if img["disp_area"] < 6_000:
            continue
        chosen.append(img["xref"])
    return chosen


def pick_founder_image(images: list[dict], reuse: dict[int, int]) -> int | None:
    unique = [img for img in images if not is_junk_image(img, reuse)]
    fallback = [
        img
        for img in images
        if img["xref"] > 0 and img["pixel_area"] >= 2500 and min(img["disp_w"], img["disp_h"]) >= 40
    ]
    usable = unique or fallback
    if not usable:
        return None
    top = [img for img in usable if img["y0"] < 480]
    pool = top or usable
    pool.sort(key=lambda img: (-img["disp_area"], img["y0"]))
    return pool[0]["xref"]


def save_pdf_png(doc: pymupdf.Document, xref: int, dest: Path) -> None:
    extracted = doc.extract_image(xref)
    image = Image.open(io.BytesIO(extracted["image"]))
    if image.mode in {"RGBA", "LA", "P"}:
        image = image.convert("RGBA")
    else:
        image = image.convert("RGB")
    dest.parent.mkdir(parents=True, exist_ok=True)
    image.save(dest, "PNG")


def extract_pdf_assets(
    doc: pymupdf.Document,
    reuse: dict[int, int],
    pages: list[int],
    slug_dir: Path,
    failures: list[str],
    slug: str,
) -> list[str]:
    saved: list[str] = []
    if len(pages) < 2:
        if pages:
            failures.append(f"{slug}: expected a two-page spread, got {pages}")
        return saved

    cover_no, profile_no = pages[0], pages[1]
    if cover_no in SKIP_PAGES or profile_no in SKIP_PAGES:
        failures.append(f"{slug}: skipped reserved pages {pages}")
        return saved
    if cover_no < 1 or profile_no > doc.page_count:
        failures.append(f"{slug}: page range {pages} outside PDF ({doc.page_count} pages)")
        return saved

    cover_page = doc[cover_no - 1]
    profile_page = doc[profile_no - 1]
    cover_xrefs = pick_cover_images(page_images(cover_page), reuse)
    for index, xref in enumerate(cover_xrefs):
        dest = slug_dir / f"pdf_cover_{index}.png"
        try:
            save_pdf_png(doc, xref, dest)
            saved.append(dest.name)
        except Exception as exc:
            failures.append(f"{slug}: pdf cover xref {xref} failed ({exc})")

    founder_xref = pick_founder_image(page_images(profile_page), reuse)
    if founder_xref is None:
        failures.append(f"{slug}: no profile-page image for founder")
        return saved
    dest = slug_dir / "pdf_founder.png"
    try:
        save_pdf_png(doc, founder_xref, dest)
        saved.append(dest.name)
    except Exception as exc:
        failures.append(f"{slug}: pdf founder xref {founder_xref} failed ({exc})")
    return saved


def rel_asset(slug: str, filename: str) -> str:
    return f"assets/{slug}/{filename}"


def merge_records(pdf_startups: list[dict], web_startups: list[dict]) -> list[dict]:
    merged: dict[str, dict] = {}

    def bucket_for(name: str) -> dict:
        canonical = resolve_canonical(name)
        # Prefer an already-created PDF bucket when a web-only prefix matches.
        key = normalize_key(canonical)
        if key not in merged:
            for existing_key, row in merged.items():
                if key.startswith(existing_key) or existing_key.startswith(key):
                    if min(len(key), len(existing_key)) >= 5:
                        return row
            merged[key] = {
                "name": canonical,
                "source_names": [],
                "pdf": None,
                "web": None,
            }
            return merged[key]
        return merged[key]

    for row in pdf_startups:
        name = row["name"]
        bucket = bucket_for(name)
        bucket["name"] = resolve_canonical(name) if normalize_key(name) in ALIAS_INDEX else name
        bucket["pdf"] = row
        bucket["source_names"].append(name)

    for row in web_startups:
        name = row["name"]
        bucket = bucket_for(name)
        if bucket["pdf"] is None and normalize_key(name) not in ALIAS_INDEX:
            bucket["name"] = name
        bucket["web"] = row
        bucket["source_names"].append(name)

    return list(merged.values())


def build_catalog(
    pdf_path: Path,
    pdf_scratch: Path,
    web_scratch: Path,
) -> tuple[dict, list[str]]:
    pdf_data = json.loads(pdf_scratch.read_text(encoding="utf-8"))
    web_data = json.loads(web_scratch.read_text(encoding="utf-8"))
    records = merge_records(pdf_data["startups"], web_data["startups"])
    records.sort(key=lambda row: slugify(row["name"]))

    known_aliases = {
        row["name"]: heuristic_aliases(row["name"], row["source_names"]) for row in records
    }
    llm_aliases = grok_aliases([row["name"] for row in records], known_aliases)

    if ASSETS_DIR.exists():
        shutil.rmtree(ASSETS_DIR)
    ASSETS_DIR.mkdir(parents=True, exist_ok=True)

    failures: list[str] = []
    doc = pymupdf.open(pdf_path)
    reuse = xref_reuse_counts(doc)
    startups: list[dict] = []

    with httpx.Client(timeout=30.0, follow_redirects=True, headers={"User-Agent": "DriveFaceIndexer/1.0"}) as client:
        for record in records:
            name = record["name"]
            slug = slugify(name)
            pdf_row = record["pdf"]
            web_row = record["web"]
            slug_dir = ASSETS_DIR / slug
            slug_dir.mkdir(parents=True, exist_ok=True)

            aliases = heuristic_aliases(name, record["source_names"])
            for extra in llm_aliases.get(name, []):
                alias = pretty_alias(str(extra))
                if alias and alias not in aliases:
                    aliases.append(alias)

            founders = merge_founders(
                list(pdf_row.get("founders") or []) if pdf_row else [],
                list(web_row.get("founders") or []) if web_row else [],
            )
            candidates = candidate_founders(pdf_row)
            pages = list(pdf_row.get("pages") or []) if pdf_row else []
            sources = [src for src, present in (("pdf", pdf_row), ("web", web_row)) if present]

            one_liner = ""
            if pdf_row and pdf_row.get("one_liner"):
                one_liner = pdf_row["one_liner"]
            elif web_row and web_row.get("description"):
                one_liner = web_row["description"]

            cohort = (pdf_row or {}).get("cohort") or None
            if not cohort and web_row and web_row.get("founder_meta"):
                cohort = web_row["founder_meta"]
            sector = (pdf_row or {}).get("sector") or None

            logos: list[str] = []
            founder_photos: list[str] = []
            if web_row and web_row.get("logo_url"):
                filename = download_file(
                    client,
                    web_row["logo_url"],
                    slug_dir / "web_logo.png",
                    failures,
                    f"{slug} web logo",
                )
                if filename:
                    logos.append(rel_asset(slug, filename))
            if web_row:
                for index, url in enumerate(web_row.get("founder_photo_urls") or []):
                    if not url:
                        continue
                    filename = download_file(
                        client,
                        url,
                        slug_dir / f"web_founder_{index}.webp",
                        failures,
                        f"{slug} web founder {index}",
                    )
                    if filename:
                        founder_photos.append(rel_asset(slug, filename))

            if pdf_row and pages:
                extracted = extract_pdf_assets(doc, reuse, pages, slug_dir, failures, slug)
                for filename in extracted:
                    path = rel_asset(slug, filename)
                    if filename.startswith("pdf_cover_"):
                        logos.append(path)
                    elif filename == "pdf_founder.png":
                        founder_photos.append(path)

            primary_logo = logos[0] if logos else None
            startups.append(
                {
                    "slug": slug,
                    "name": name,
                    "aliases": aliases,
                    "cohort": cohort,
                    "sector": sector,
                    "one_liner": one_liner,
                    "founders": founders,
                    "candidate_founders": candidates,
                    "pages": pages,
                    "assets": {
                        "logo": primary_logo,
                        "logos": logos,
                        "founder_photos": founder_photos,
                    },
                    "sources": sources,
                }
            )

    doc.close()
    catalog = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "sources": ["pdf", "web"],
        "startups": startups,
    }
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    CATALOG_PATH.write_text(json.dumps(catalog, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return catalog, failures


def summarize(catalog: dict) -> tuple[int, int, int]:
    startups = catalog["startups"]
    logo_count = sum(1 for row in startups if row["assets"].get("logo"))
    founder_count = sum(1 for row in startups if row["assets"].get("founder_photos"))
    return len(startups), logo_count, founder_count


def main() -> int:
    load_dotenv_files()
    pdf_path = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_PDF
    pdf_scratch = Path(os.environ.get("PDF_SCRATCH", DEFAULT_PDF_SCRATCH))
    web_scratch = Path(os.environ.get("WEB_SCRATCH", DEFAULT_WEB_SCRATCH))
    for required in (pdf_path, pdf_scratch, web_scratch):
        if not required.is_file():
            print(f"Missing required file: {required}", file=sys.stderr)
            return 1

    catalog, failures = build_catalog(pdf_path, pdf_scratch, web_scratch)
    total, logos, founders = summarize(catalog)
    print(f"catalog: {CATALOG_PATH}")
    print(f"startups: {total}")
    print(f"with logo: {logos}")
    print(f"with founder photos: {founders}")
    if failures:
        print(f"failures: {len(failures)}")
        for item in failures:
            print(f"  - {item}")
    else:
        print("failures: 0")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
