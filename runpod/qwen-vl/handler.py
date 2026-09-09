"""RunPod serverless handler: Qwen3-VL identify via local SGLang. No DB writes."""

from __future__ import annotations

import asyncio
import logging
import os
import time

import httpx
import runpod

from tags import IDENTIFY_AND_CAPTION_PROMPT, filter_tags

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("dfi-qwen-vl")

SGLANG = os.environ.get("SGLANG_URL", "http://127.0.0.1:8000").rstrip("/")
MODEL = os.environ.get("QWEN_MODEL", "Qwen/Qwen3-VL-8B-Instruct")
CONCURRENCY = max(1, int(os.environ.get("QWEN_CONCURRENCY", "32")))

_client: httpx.AsyncClient | None = None
_gate = asyncio.Semaphore(CONCURRENCY)


def _http() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(
            timeout=httpx.Timeout(180.0, connect=10.0),
            limits=httpx.Limits(
                max_connections=CONCURRENCY + 8,
                max_keepalive_connections=CONCURRENCY,
            ),
        )
    return _client


def _payload(image_b64: str, prompt: str, max_tokens: int) -> dict:
    return {
        "model": MODEL,
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/jpeg;base64,{image_b64}"},
                    },
                    {"type": "text", "text": prompt},
                ],
            }
        ],
        "max_tokens": max_tokens,
        "temperature": 0.1,
    }


_sglang_ready = False


async def _wait_sglang(timeout_s: float = 1500.0) -> dict:
    global _sglang_ready
    if _sglang_ready:
        resp = await _http().get(f"{SGLANG}/v1/models")
        resp.raise_for_status()
        return resp.json()
    deadline = time.monotonic() + timeout_s
    last = ""
    while time.monotonic() < deadline:
        try:
            resp = await _http().get(f"{SGLANG}/v1/models")
            if resp.status_code == 200:
                _sglang_ready = True
                logger.info("sglang ready")
                return resp.json()
            last = f"{resp.status_code} {resp.text[:160]}"
        except Exception as exc:  # noqa: BLE001
            last = str(exc)[:200]
        await asyncio.sleep(5.0)
    raise RuntimeError(f"SGLang not ready after {timeout_s:.0f}s: {last}")


async def _identify_image(image_b64: str, prompt: str, max_tokens: int) -> dict:
    t0 = time.perf_counter()
    async with _gate:
        resp = await _http().post(
            f"{SGLANG}/v1/chat/completions",
            json=_payload(image_b64, prompt, max_tokens),
        )
    elapsed = time.perf_counter() - t0
    if resp.status_code >= 400:
        return {
            "error": f"Identify failed {resp.status_code}: {resp.text[:500]}",
            "identify_s": round(elapsed, 3),
            "tags": [],
            "raw_text": "",
        }
    raw = resp.json()
    text = (((raw.get("choices") or [{}])[0].get("message") or {}).get("content") or "").strip()
    return {
        "model": MODEL,
        "tags": filter_tags(text),
        "raw_text": text,
        "identify_s": round(elapsed, 3),
        "usage": raw.get("usage"),
        "error": None,
    }


async def handler(job: dict) -> dict:
    inp = job.get("input") or {}
    if inp.get("healthcheck"):
        models = await _wait_sglang()
        return {"ok": True, "models": models, "concurrency": CONCURRENCY}

    await _wait_sglang()
    prompt = str(inp.get("prompt") or IDENTIFY_AND_CAPTION_PROMPT)
    max_tokens = int(inp.get("max_tokens") or os.environ.get("QWEN_MAX_TOKENS", "1536"))
    images = inp.get("images")
    if isinstance(images, list) and images:
        async def _one(item: dict, idx: int) -> dict:
            b64 = str((item or {}).get("image_b64") or "").strip()
            if not b64:
                return {"index": idx, "error": "image_b64 is required", "tags": []}
            out = await _identify_image(b64, prompt, max_tokens)
            out["index"] = (item or {}).get("index", idx)
            out["name"] = (item or {}).get("name")
            return out

        results = await asyncio.gather(*[_one(item, i) for i, item in enumerate(images)])
        return {"results": list(results), "count": len(results), "concurrency": CONCURRENCY}

    image_b64 = str(inp.get("image_b64") or "").strip()
    if not image_b64:
        return {"error": "image_b64 is required"}
    return await _identify_image(image_b64, prompt, max_tokens)


def _concurrency(_: int) -> int:
    return CONCURRENCY


if __name__ == "__main__":
    logger.info("starting qwen identify worker concurrency=%s sglang=%s", CONCURRENCY, SGLANG)
    runpod.serverless.start(
        {
            "handler": handler,
            "concurrency_modifier": _concurrency,
        }
    )
