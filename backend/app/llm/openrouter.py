"""OpenRouter OpenAI-compatible chat completions (JSON)."""

from __future__ import annotations

import json
import logging
import re
from typing import Any

import httpx

logger = logging.getLogger(__name__)

_DEFAULT_BASE = "https://openrouter.ai/api/v1"
_DEFAULT_TIMEOUT = 120.0
# Thinking Gemini 3.x burns a large share of max_tokens before JSON starts.
_GEMINI_MAX_TOKENS_FLOOR = 4096
_THINKING_GEMINI_RE = re.compile(r"gemini-(?:2\.5|3)", re.I)


def is_google_gemini_model(model_id: str) -> bool:
    mid = (model_id or "").strip().lower()
    if not mid:
        return False
    leaf = mid.rsplit("/", 1)[-1]
    return mid.startswith("google/") or leaf.startswith("gemini")


def is_thinking_gemini_model(model_id: str) -> bool:
    return bool(_THINKING_GEMINI_RE.search((model_id or "").strip()))


def _fold_system_into_user(system: str, prompt: str) -> list[dict[str, str]]:
    sys_msg = (system or "").strip()
    user = (prompt or "").strip()
    if sys_msg:
        user = f"{sys_msg}\n\n{user}" if user else sys_msg
    return [{"role": "user", "content": user}]


def _assistant_text(payload: dict[str, Any]) -> str:
    """Pull visible text from OpenRouter / Gemini message shapes."""
    choices = payload.get("choices") if isinstance(payload, dict) else None
    if not isinstance(choices, list) or not choices:
        return ""
    first = choices[0] if isinstance(choices[0], dict) else {}
    message = first.get("message") if isinstance(first.get("message"), dict) else {}
    chunks: list[str] = []

    def _take(value: Any) -> None:
        if isinstance(value, str) and value.strip():
            chunks.append(value.strip())
        elif isinstance(value, list):
            for part in value:
                if isinstance(part, dict):
                    text = part.get("text") or part.get("content")
                    if isinstance(text, str) and text.strip():
                        chunks.append(text.strip())
                elif isinstance(part, str) and part.strip():
                    chunks.append(part.strip())

    _take(message.get("content"))
    if not chunks:
        # Gemini thinking models often leave content empty and fill reasoning.
        _take(message.get("reasoning"))
        _take(message.get("reasoning_content"))
        _take(message.get("reasoning_details"))
        _take(first.get("text"))
    return "\n".join(chunks).strip()


def _request_bodies(
    *,
    model_id: str,
    prompt: str,
    system: str,
    temperature: float,
    max_tokens: int,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Primary body (structured JSON) plus a stripped retry body."""
    gemini = is_google_gemini_model(model_id)
    thinking = is_thinking_gemini_model(model_id)
    token_budget = max(int(max_tokens or 0), _GEMINI_MAX_TOKENS_FLOOR) if gemini else max_tokens
    if gemini:
        messages = _fold_system_into_user(system, prompt)
    else:
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": prompt},
        ]
    base_body: dict[str, Any] = {
        "model": model_id,
        "messages": messages,
        "max_tokens": token_budget,
    }
    # Gemini 3.x rejects or ignores sampling overrides; omit so the hop 200s.
    if not thinking:
        base_body["temperature"] = temperature
    if thinking:
        # Default Gemini 3 effort is medium (~50% of max_tokens) and slow.
        # Low keeps JSON in the remaining budget and finishes like Claude.
        base_body["reasoning"] = {"effort": "low", "exclude": True}

    request_body = {**base_body, "response_format": {"type": "json_object"}}
    # Keep low-reasoning on retry so Gemini 3 does not snap back to default medium thinking.
    retry_body = dict(base_body)
    return request_body, retry_body


def _request_bodies_vision(
    *,
    model_id: str,
    content: list[dict[str, Any]],
    system: str,
    temperature: float,
    max_tokens: int,
) -> tuple[dict[str, Any], dict[str, Any]]:
    gemini = is_google_gemini_model(model_id)
    thinking = is_thinking_gemini_model(model_id)
    user_content = list(content)
    sys_msg = (system or "").strip()
    if gemini:
        if sys_msg:
            if user_content and user_content[0].get("type") == "text":
                first = dict(user_content[0])
                first["text"] = f"{sys_msg}\n\n{first.get('text') or ''}"
                user_content = [first, *user_content[1:]]
            else:
                user_content = [{"type": "text", "text": sys_msg}, *user_content]
        messages: list[dict[str, Any]] = [{"role": "user", "content": user_content}]
    else:
        messages = [
            {"role": "system", "content": sys_msg or "Return ONLY valid JSON."},
            {"role": "user", "content": user_content},
        ]
    token_budget = max(int(max_tokens or 0), 2048)
    if gemini:
        token_budget = max(token_budget, _GEMINI_MAX_TOKENS_FLOOR)
    base_body: dict[str, Any] = {
        "model": model_id,
        "messages": messages,
        "max_tokens": token_budget,
    }
    if not thinking:
        base_body["temperature"] = temperature
    if thinking:
        base_body["reasoning"] = {"effort": "low", "exclude": True}
    request_body = {**base_body, "response_format": {"type": "json_object"}}
    return request_body, dict(base_body)


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
    json_root: str = "object",
) -> str:
    """POST ``{base}/chat/completions`` and return assistant text.

    Uses ``response_format: json_object`` when accepted; array responses are
    requested through an ``{"items": [...]}`` wrapper and unwrapped before
    returning. On format rejection, empty Gemini content, or a generic 400,
    retries without structured output / thinking extras.
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
    root = "array" if (json_root or "").strip().lower() == "array" else "object"
    if root == "array":
        sys_msg = (
            f"{sys_msg} Return a top-level JSON object with exactly one key, "
            '"items", whose value is the requested JSON array.'
        )

    request_body, retry_body = _request_bodies(
        model_id=model_id,
        prompt=prompt,
        system=sys_msg,
        temperature=temperature,
        max_tokens=max_tokens,
    )
    async with httpx.AsyncClient(timeout=timeout) as client:
        text = await _post_once(
            client,
            url,
            headers=headers,
            body=request_body,
            allow_format_retry=True,
            retry_body=retry_body,
        )
    if root == "array":
        try:
            wrapped = json.loads(text)
        except json.JSONDecodeError:
            return text
        if isinstance(wrapped, list):
            return text
        if isinstance(wrapped, dict):
            for wrap_key in ("items", "themes", "results"):
                items = wrapped.get(wrap_key)
                if isinstance(items, list):
                    return json.dumps(items, ensure_ascii=False)
    return text


def complete_vision_json_sync(
    content: list[dict[str, Any]],
    *,
    model: str,
    api_key: str,
    base_url: str = _DEFAULT_BASE,
    system: str = "Return ONLY valid JSON.",
    temperature: float = 0.1,
    max_tokens: int = 2048,
    timeout: float = 30.0,
) -> str:
    """Synchronous multimodal chat/completions for frame ranking."""
    key = (api_key or "").strip()
    model_id = (model or "").strip()
    if not key:
        raise RuntimeError("OpenRouter API key is empty")
    if not model_id:
        raise RuntimeError("OpenRouter model id is empty")
    if not content:
        raise RuntimeError("OpenRouter vision content is empty")

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
    request_body, retry_body = _request_bodies_vision(
        model_id=model_id,
        content=content,
        system=sys_msg,
        temperature=temperature,
        max_tokens=max_tokens,
    )
    with httpx.Client(timeout=timeout) as client:
        return _post_once_sync(
            client,
            url,
            headers=headers,
            body=request_body,
            allow_format_retry=True,
            retry_body=retry_body,
        )


def _post_once_sync(
    client: httpx.Client,
    url: str,
    *,
    headers: dict[str, str],
    body: dict[str, Any],
    allow_format_retry: bool,
    retry_body: dict[str, Any],
) -> str:
    try:
        resp = client.post(url, headers=headers, json=body)
    except httpx.HTTPError as exc:
        logger.warning("OpenRouter request failed: %s", str(exc)[:200])
        raise RuntimeError(f"OpenRouter request failed: {exc}") from exc
    return _read_openrouter_response(
        resp,
        body=body,
        allow_format_retry=allow_format_retry,
        retry=lambda: _post_once_sync(
            client,
            url,
            headers=headers,
            body=retry_body,
            allow_format_retry=False,
            retry_body=retry_body,
        ),
    )


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

    async def _retry() -> str:
        return await _post_once(
            client,
            url,
            headers=headers,
            body=retry_body,
            allow_format_retry=False,
            retry_body=retry_body,
        )

    text_or_retry = _read_openrouter_response(
        resp,
        body=body,
        allow_format_retry=allow_format_retry,
        retry=_retry,
    )
    if hasattr(text_or_retry, "__await__"):
        return await text_or_retry  # type: ignore[misc]
    return text_or_retry


def _read_openrouter_response(
    resp: httpx.Response,
    *,
    body: dict[str, Any],
    allow_format_retry: bool,
    retry,
) -> str:
    if resp.status_code >= 400:
        detail = (resp.text or "")[:300]
        if allow_format_retry and resp.status_code in (400, 422):
            logger.info(
                "OpenRouter HTTP %s model=%s; retrying without extras: %s",
                resp.status_code,
                body.get("model"),
                detail[:160],
            )
            return retry()
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

    if not isinstance(data, dict) or not isinstance(data.get("choices"), list) or not data["choices"]:
        logger.warning("OpenRouter empty choices: %s", str(data)[:200])
        raise RuntimeError("OpenRouter returned no choices")

    text = _assistant_text(data)
    if not text and allow_format_retry:
        logger.info(
            "OpenRouter empty content model=%s; retrying without extras",
            body.get("model"),
        )
        return retry()
    if not text:
        raise RuntimeError("OpenRouter returned empty content")
    return text
