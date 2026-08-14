"""OpenRouter OpenAI-compatible chat completions (JSON)."""

from __future__ import annotations

import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)

_DEFAULT_BASE = "https://openrouter.ai/api/v1"
_DEFAULT_TIMEOUT = 120.0


async def complete_json(
    prompt: str,
    *,
    model: str,
    api_key: str,
    base_url: str = _DEFAULT_BASE,
    system: str = "Return ONLY valid JSON.",
    temperature: float = 0.3,
    max_tokens: int = 4096,
    timeout: float = _DEFAULT_TIMEOUT,
) -> str:
    """POST ``{base}/chat/completions`` and return assistant text.

    Uses ``response_format: json_object`` when accepted; on format rejection
    retries without it (JSON still requested in the system/user prompt).
    """
    key = (api_key or "").strip()
    model_id = (model or "").strip()
    if not key:
        raise RuntimeError("OpenRouter API key is empty")
    if not model_id:
        raise RuntimeError("OpenRouter model id is empty")

    url = f"{(base_url or _DEFAULT_BASE).rstrip('/')}/chat/completions"
    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://github.com/video-image-search-indexer",
        "X-Title": "carousel-llm",
    }
    sys_msg = (system or "").strip() or "Return ONLY valid JSON."
    if "json" not in sys_msg.lower():
        sys_msg = f"{sys_msg} Return ONLY valid JSON."

    messages: list[dict[str, str]] = [
        {"role": "system", "content": sys_msg},
        {"role": "user", "content": prompt},
    ]
    base_body: dict[str, Any] = {
        "model": model_id,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }

    async with httpx.AsyncClient(timeout=timeout) as client:
        text = await _post_once(
            client,
            url,
            headers=headers,
            body={**base_body, "response_format": {"type": "json_object"}},
            allow_format_retry=True,
            retry_body=base_body,
        )
    return text


async def _post_once(
    client: httpx.AsyncClient,
    url: str,
    *,
    headers: dict[str, str],
    body: dict[str, Any],
    allow_format_retry: bool,
    retry_body: dict[str, Any],
) -> str:
    try:
        resp = await client.post(url, headers=headers, json=body)
    except httpx.HTTPError as exc:
        logger.warning("OpenRouter request failed: %s", str(exc)[:200])
        raise RuntimeError(f"OpenRouter request failed: {exc}") from exc

    if resp.status_code >= 400:
        detail = (resp.text or "")[:300]
        # Some models reject response_format — retry without it.
        if (
            allow_format_retry
            and resp.status_code == 400
            and "response_format" in detail.lower()
        ):
            logger.info("OpenRouter rejected response_format; retrying without it")
            return await _post_once(
                client,
                url,
                headers=headers,
                body=retry_body,
                allow_format_retry=False,
                retry_body=retry_body,
            )
        logger.warning(
            "OpenRouter HTTP %s model=%s: %s",
            resp.status_code,
            body.get("model"),
            detail,
        )
        raise RuntimeError(f"OpenRouter HTTP {resp.status_code}: {detail}")

    try:
        data = resp.json()
    except Exception as exc:  # noqa: BLE001
        logger.warning("OpenRouter non-JSON response: %s", (resp.text or "")[:200])
        raise RuntimeError("OpenRouter returned non-JSON body") from exc

    choices = data.get("choices") if isinstance(data, dict) else None
    if not isinstance(choices, list) or not choices:
        logger.warning("OpenRouter empty choices: %s", str(data)[:200])
        raise RuntimeError("OpenRouter returned no choices")

    message = choices[0].get("message") if isinstance(choices[0], dict) else None
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, list):
        # Rare multimodal-style content blocks
        parts = [
            str(p.get("text") or "")
            for p in content
            if isinstance(p, dict)
        ]
        content = "".join(parts)
    text = (content or "").strip() if isinstance(content, str) else ""
    if not text:
        raise RuntimeError("OpenRouter returned empty content")
    return text
