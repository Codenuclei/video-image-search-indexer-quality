"""Local search-quality evaluator for DriveFaceIndexer.

Runs a fixed golden hard-query set against the live backend, downloads every
result's preview, verifies it loads, and uses Gemini vision to judge whether the
image GENUINELY matches the query (strict true/false). Computes the exact 95%
confidence rule and writes a timestamped JSON + markdown report.

No secrets are printed or committed. The Gemini key is read from backend/.env.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import io
import json
import os
import sys
from pathlib import Path

import httpx

BACKEND_DIR = Path(__file__).resolve().parents[1]
REPO_DIR = BACKEND_DIR.parent
DEFAULT_BASE = "https://dfi-backend-production.up.railway.app"

GOLDEN = [
    "flying car",
    "car lifted in the air",
    "laser blast",
    "students playing sports outdoors",
    "graduation ceremony on stage",
    "person giving a presentation with a projector",
    "crowd celebrating with confetti",
    "aerial drone shot of the campus",
    "people running a race",
    "someone lifting a barbell / weightlifting",
    "students studying in a classroom",
    "group photo of people smiling",
    "placement report statistics chart",
    "curriculum overview page",
    "building exterior / campus architecture",
]


def load_gemini_key() -> str | None:
    env = BACKEND_DIR / ".env"
    if not env.exists():
        return os.environ.get("GEMINI_API_KEY")
    for line in env.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = line.strip()
        if line.startswith("GEMINI_API_KEY="):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    return os.environ.get("GEMINI_API_KEY")


def decode_ok(data: bytes) -> bool:
    if not data:
        return False
    try:
        from PIL import Image  # type: ignore

        Image.open(io.BytesIO(data)).verify()
        return True
    except Exception:
        # Fallback: rely on non-trivial byte length + common image magic bytes.
        return len(data) > 512 and (
            data[:3] == b"\xff\xd8\xff" or data[:8] == b"\x89PNG\r\n\x1a\n" or data[:4] == b"RIFF"
        )


def preview_url(base: str, kind: str, ident) -> str | None:
    if kind == "file":
        return f"{base}/drive/files/{ident}/preview"
    if kind == "moment":
        if not ident:
            return None
        return ident if str(ident).startswith("http") else f"{base}{ident}"
    return None


def gemini_true_match(client, query: str, data: bytes) -> bool:
    """Strict yes/no: does the image genuinely depict the query? Retries on transient errors."""
    import time

    from google.genai import types

    prompt = (
        f"Question: does this image GENUINELY and clearly depict: '{query}'?\n"
        "Answer with a single word: YES only if it is a true, unambiguous match; "
        "NO if it is unrelated, only loosely related, or ambiguous. One word only."
    )
    last_exc = None
    for attempt in range(4):
        try:
            resp = client.models.generate_content(
                model="gemini-2.5-flash",
                contents=[types.Part.from_bytes(data=data, mime_type="image/jpeg"), prompt],
            )
            text = (resp.text or "").strip().upper()
            return text.startswith("YES")
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            msg = str(exc)
            if any(code in msg for code in ("503", "UNAVAILABLE", "429", "500", "RESOURCE_EXHAUSTED")):
                time.sleep(2 * (attempt + 1))
                continue
            break
    print(f"    [warn] gemini judge failed after retries: {last_exc}", file=sys.stderr)
    return False


def fetch_results(client_http: httpx.Client, base: str, query: str) -> list[dict]:
    """Return a merged ranked list of results: files first, then video moments."""
    r = client_http.get(
        f"{base}/search", params={"q": query, "mime": "all", "rerank": "true"}, timeout=120
    )
    r.raise_for_status()
    payload = r.json()
    results: list[dict] = []
    for f in payload.get("files", []):
        results.append(
            {
                "kind": "file",
                "id": f.get("drive_file_id"),
                "name": f.get("name"),
                "preview": preview_url(base, "file", f.get("drive_file_id")),
            }
        )
    for m in payload.get("moments", []):
        results.append(
            {
                "kind": "moment",
                "id": m.get("drive_file_id"),
                "name": m.get("name"),
                "preview": preview_url(base, "moment", m.get("preview_url")),
            }
        )
    return results


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", default=os.environ.get("DFI_BASE_URL", DEFAULT_BASE))
    ap.add_argument("--judge-top", type=int, default=5, help="results per query to vision-judge")
    ap.add_argument("--people", type=int, default=5, help="people queries to add from /persons")
    args = ap.parse_args()

    base = args.base_url.rstrip("/")
    key = load_gemini_key()
    if not key:
        print("ERROR: no GEMINI_API_KEY found (backend/.env or env)", file=sys.stderr)
        return 2

    from google import genai

    gclient = genai.Client(api_key=key)
    http = httpx.Client(follow_redirects=True)

    queries = list(GOLDEN)
    try:
        pr = http.get(f"{base}/persons", timeout=60)
        if pr.status_code == 200:
            names = [p.get("name") for p in pr.json() if p.get("name")]
            for n in names[: args.people]:
                queries.append(f"photos of {n}")
    except Exception:
        pass

    report = []
    passing = 0
    any_top3_fp = False

    for q in queries:
        entry = {"query": q, "results": [], "pass": False, "note": ""}
        try:
            results = fetch_results(http, base, q)
        except Exception as exc:  # noqa: BLE001
            entry["note"] = f"search failed: {exc}"
            report.append(entry)
            print(f"[FAIL] {q}: search error {exc}")
            continue

        judged = results[: args.judge_top]
        truths: list[bool] = []
        for idx, res in enumerate(judged):
            preview_ok = False
            true_match = False
            if res.get("preview"):
                try:
                    pr2 = http.get(res["preview"], timeout=90)
                    if pr2.status_code == 200 and decode_ok(pr2.content):
                        preview_ok = True
                        true_match = gemini_true_match(gclient, q, pr2.content)
                except Exception:
                    preview_ok = False
            valid = preview_ok and true_match
            truths.append(valid)
            entry["results"].append(
                {
                    "rank": idx + 1,
                    "kind": res["kind"],
                    "name": res.get("name"),
                    "preview_ok": preview_ok,
                    "true_match": true_match,
                }
            )

        n = len(judged)
        top5 = truths[:5]
        top3 = truths[:3]
        top3_fp = any(not t for t in top3) if top3 else True
        if top3_fp:
            any_top3_fp = True

        if n == 0:
            entry["pass"] = False
            entry["note"] = "no results"
        elif n < 5:
            entry["pass"] = all(truths)
            entry["note"] = "fewer than 5 candidates"
        else:
            entry["pass"] = all(top5) and not top3_fp

        if entry["pass"]:
            passing += 1
        report.append(entry)
        print(f"[{'PASS' if entry['pass'] else 'FAIL'}] {q}  top5={sum(top5)}/{min(5,n)}")

    total = len(queries)
    confidence = (passing / total) if total else 0.0
    achieved = confidence >= 0.95 and not any_top3_fp

    stamp = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    out_dir = REPO_DIR / "logs" / "quality"
    out_dir.mkdir(parents=True, exist_ok=True)
    summary = {
        "timestamp": stamp,
        "base_url": base,
        "total_queries": total,
        "passing": passing,
        "confidence": round(confidence, 4),
        "achieved_95": achieved,
        "any_top3_false_positive": any_top3_fp,
        "queries": report,
    }
    (out_dir / f"eval-{stamp}.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")

    lines = [
        f"# Search Quality Eval {stamp}",
        f"- Confidence: **{confidence*100:.1f}%** ({passing}/{total})",
        f"- ACHIEVED (>=95%, no top-3 FP): **{achieved}**",
        "",
        "| Query | Pass | top5 true |",
        "|---|---|---|",
    ]
    for e in report:
        t5 = sum(1 for r in e["results"][:5] if r.get("true_match") and r.get("preview_ok"))
        lines.append(f"| {e['query']} | {'PASS' if e['pass'] else 'FAIL'} | {t5}/5 |")
    (out_dir / f"eval-{stamp}.md").write_text("\n".join(lines), encoding="utf-8")

    with (out_dir / "summary.log").open("a", encoding="utf-8") as fh:
        fh.write(
            f"{stamp} confidence={confidence*100:.1f}% ({passing}/{total}) achieved={achieved}\n"
        )

    print(
        f"\nCONFIDENCE={confidence*100:.1f}% ({passing}/{total}) ACHIEVED={achieved} "
        f"-> logs/quality/eval-{stamp}.md"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
