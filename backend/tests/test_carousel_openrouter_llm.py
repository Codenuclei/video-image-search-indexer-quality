"""OpenRouter carousel LLM: provider resolution + httpx helper (no network)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from app.llm.carousel_llm import (
    CAROUSEL_LLM_MODEL_OPTIONS,
    normalize_carousel_llm_provider,
    openrouter_slug_for_direct,
    prefer_openrouter_first,
    resolve_carousel_llm,
)
from app.runtime_settings import RuntimeSettings, set_runtime_settings
from app.search.carousel_pipeline import _llm_complete_json, _llm_has_any_key


def _runtime(**overrides: object) -> RuntimeSettings:
    base = RuntimeSettings(
        auto_index_enabled=False,
        auto_index_interval_seconds=30,
        reindex_errored_files=False,
        reindex_skipped_files=False,
        follow_shortcut_folders=True,
        experimental_manual_face_tag=False,
        gemini_file_search_search_enabled=False,
        search_parallel_variants_enabled=False,
        search_use_captions=True,
        search_rerank_enabled=False,
        search_semantic_min_score=0.32,
        go_indexer_enabled=False,
        carousel_llm_provider="auto",
        openrouter_model="anthropic/claude-sonnet-4",
        claude_model="claude-sonnet-4-5-20250929",
    )
    for key, value in overrides.items():
        setattr(base, key, value)
    return base


def test_normalize_carousel_llm_provider():
    assert normalize_carousel_llm_provider("OpenRouter") == "openrouter"
    assert normalize_carousel_llm_provider("bogus") == "auto"
    assert normalize_carousel_llm_provider(None) == "auto"


def test_resolve_carousel_llm_packs_runtime_and_env():
    set_runtime_settings(
        _runtime(
            carousel_llm_provider="openrouter",
            openrouter_model="google/gemini-2.5-flash",
        )
    )
    fake = MagicMock()
    fake.gemini_api_key = "g-key"
    fake.gemini_model = "gemini-2.5-flash"
    fake.anthropic_api_key = "a-key"
    fake.claude_api_key = ""
    fake.claude_model = "claude-sonnet-4-20250514"
    fake.openrouter_api_key = "or-key"
    fake.openrouter_model = "anthropic/claude-sonnet-4"
    fake.openrouter_base_url = "https://openrouter.ai/api/v1"

    with patch("app.llm.carousel_llm.get_settings", return_value=fake):
        pack = resolve_carousel_llm()

    assert pack["provider"] == "openrouter"
    assert pack["openrouter_model"] == "google/gemini-2.5-flash"
    assert pack["openrouter_api_key"] == "or-key"
    assert pack["api_key"] == "g-key"
    assert pack["claude_api_key"] == "a-key"
    assert any(o["id"] == "anthropic/claude-sonnet-4" for o in CAROUSEL_LLM_MODEL_OPTIONS)


def test_llm_has_any_key_requires_openrouter_model():
    assert not _llm_has_any_key(openrouter_api_key="k", openrouter_model="")
    assert _llm_has_any_key(openrouter_api_key="k", openrouter_model="m")
    assert _llm_has_any_key(claude_api_key="c")


@pytest.mark.asyncio
async def test_llm_complete_json_auto_prefers_claude(monkeypatch):
    called: list[str] = []

    async def fake_or(*_a, **_k):
        called.append("openrouter")
        return '{"ok": true}'

    class _Block:
        type = "text"
        text = '{"ok": true}'

    class _Resp:
        content = [_Block()]

    class _Messages:
        def create(self, **_kwargs):
            called.append("claude")
            return _Resp()

    class _Client:
        def __init__(self, *args, **kwargs):
            self.messages = _Messages()

    monkeypatch.setattr("app.llm.openrouter.complete_json", fake_or)
    monkeypatch.setattr("anthropic.Anthropic", _Client)

    text, provider = await _llm_complete_json(
        prompt='Return {"ok":true}',
        provider="auto",
        openrouter_api_key="or-key",
        openrouter_model="anthropic/claude-sonnet-4",
        openrouter_base_url="https://openrouter.ai/api/v1",
        claude_api_key="claude-key",
        claude_model="claude-sonnet-4-5-20250929",
        api_key="gemini-key",
        model="gemini-2.5-flash",
    )
    assert provider == "claude"
    assert "ok" in text
    assert called == ["claude"]


@pytest.mark.asyncio
async def test_llm_complete_json_claude_skips_openrouter_and_gemini(monkeypatch):
    async def boom_or(*_a, **_k):
        raise AssertionError("openrouter must not run for provider=claude")

    monkeypatch.setattr("app.llm.openrouter.complete_json", boom_or)

    class _Block:
        type = "text"
        text = '{"themes":[]}'

    class _Resp:
        content = [_Block()]

    class _Messages:
        def create(self, **_kwargs):
            assert _kwargs["model"] == "claude-sonnet-4-5-20250929"
            return _Resp()

    class _Client:
        def __init__(self, *args, **kwargs):
            self.messages = _Messages()

    monkeypatch.setattr("anthropic.Anthropic", _Client)

    text, provider = await _llm_complete_json(
        prompt="{}",
        provider="claude",
        openrouter_api_key="or-key",
        openrouter_model="anthropic/claude-sonnet-4",
        claude_api_key="claude-key",
        claude_model="claude-sonnet-4-5-20250929",
        api_key="gemini-key",
        model="gemini-2.5-flash",
    )
    assert provider == "claude"
    assert "themes" in text


def test_resolve_carousel_llm_honors_openrouter_choice():
    set_runtime_settings(
        _runtime(
            carousel_llm_provider="openrouter",
            openrouter_model="google/gemini-2.5-flash",
            claude_model="claude-sonnet-4-5-20250929",
        )
    )
    fake = MagicMock()
    fake.gemini_api_key = "g-key"
    fake.gemini_model = "gemini-2.5-flash"
    fake.anthropic_api_key = "a-key"
    fake.claude_api_key = ""
    fake.claude_model = "claude-sonnet-4-5-20250929"
    fake.openrouter_api_key = "or-key"
    fake.openrouter_model = "anthropic/claude-sonnet-4"
    fake.openrouter_base_url = "https://openrouter.ai/api/v1"

    with patch("app.llm.carousel_llm.get_settings", return_value=fake):
        pack = resolve_carousel_llm()

    assert pack["provider"] == "openrouter"
    assert pack["openrouter_model"] == "google/gemini-2.5-flash"


def test_openrouter_slug_for_direct_arena_ids():
    assert openrouter_slug_for_direct("claude", "claude-fable-5") == "anthropic/claude-fable-5"
    assert openrouter_slug_for_direct("claude", "claude-opus-4-6") == "anthropic/claude-opus-4.6"
    assert openrouter_slug_for_direct("gemini", "gemini-3.7-flash") == "google/gemini-3.7-flash"
    assert (
        openrouter_slug_for_direct("claude", "anthropic/claude-sonnet-4.5")
        == "anthropic/claude-sonnet-4.5"
    )
    assert prefer_openrouter_first("gemini", "gemini-3.7-flash") is True
    assert prefer_openrouter_first("claude", "claude-fable-5") is True
    assert prefer_openrouter_first("claude", "claude-sonnet-4-5-20250929") is False


@pytest.mark.asyncio
async def test_llm_complete_json_gemini_uses_openrouter_first(monkeypatch):
    called: list[str] = []

    async def fake_or(*_a, model="", **_k):
        called.append(f"openrouter:{model}")
        return '{"ok": true}'

    async def boom_gemini(*_a, **_k):
        called.append("gemini")
        raise AssertionError("direct Gemini must not run when OpenRouter is ready")

    monkeypatch.setattr("app.llm.openrouter.complete_json", fake_or)
    monkeypatch.setattr("google.genai.Client", boom_gemini)

    text, provider = await _llm_complete_json(
        prompt="{}",
        provider="gemini",
        openrouter_api_key="or-key",
        openrouter_model="anthropic/claude-sonnet-4",
        claude_api_key="",
        claude_model="",
        api_key="gemini-key",
        model="gemini-3.7-flash",
    )
    assert provider == "openrouter"
    assert called == ["openrouter:google/gemini-3.7-flash"]


@pytest.mark.asyncio
async def test_llm_complete_json_claude_unknown_model_uses_openrouter(monkeypatch):
    called: list[str] = []

    class _Messages:
        def create(self, **_kwargs):
            called.append("claude")
            raise RuntimeError("model: claude-fable-5: model not found")

    class _Client:
        def __init__(self, *args, **kwargs):
            self.messages = _Messages()

    async def fake_or(*_a, model="", **_k):
        called.append(f"openrouter:{model}")
        return '{"ok": true}'

    monkeypatch.setattr("anthropic.Anthropic", _Client)
    monkeypatch.setattr("app.llm.openrouter.complete_json", fake_or)

    text, provider = await _llm_complete_json(
        prompt="{}",
        provider="claude",
        openrouter_api_key="or-key",
        openrouter_model="anthropic/claude-sonnet-4",
        claude_api_key="claude-key",
        claude_model="claude-fable-5",
        api_key="gemini-key",
        model="gemini-2.5-flash",
    )
    assert provider == "openrouter"
    assert "ok" in text
    assert called == ["openrouter:anthropic/claude-fable-5"]


@pytest.mark.asyncio
async def test_llm_complete_json_gemini_missing_key_uses_openrouter(monkeypatch):
    async def fake_or(*_a, model="", **_k):
        assert model == "google/gemini-3.7-flash"
        return '{"ok": true}'

    monkeypatch.setattr("app.llm.openrouter.complete_json", fake_or)

    text, provider = await _llm_complete_json(
        prompt="{}",
        provider="gemini",
        openrouter_api_key="or-key",
        openrouter_model="anthropic/claude-sonnet-4",
        claude_api_key="",
        claude_model="",
        api_key="",
        model="gemini-3.7-flash",
    )
    assert provider == "openrouter"
    assert "ok" in text


@pytest.mark.asyncio
async def test_llm_complete_json_openrouter_falls_back_to_claude(monkeypatch):
    async def fail_or(*_a, **_k):
        raise RuntimeError("openrouter down")

    monkeypatch.setattr("app.llm.openrouter.complete_json", fail_or)

    class _Block:
        type = "text"
        text = '{"hooks":[]}'

    class _Resp:
        content = [_Block()]

    class _Messages:
        def create(self, **_kwargs):
            return _Resp()

    class _Client:
        def __init__(self, *args, **kwargs):
            self.messages = _Messages()

    monkeypatch.setattr("anthropic.Anthropic", _Client)

    text, provider = await _llm_complete_json(
        prompt="{}",
        provider="openrouter",
        openrouter_api_key="or-key",
        openrouter_model="anthropic/claude-sonnet-4",
        claude_api_key="claude-key",
        claude_model="claude-x",
        api_key="",
        model="",
    )
    assert provider == "claude"
    assert "hooks" in text


@pytest.mark.asyncio
async def test_openrouter_complete_json_mocks_httpx(monkeypatch):
    from app.llm import openrouter as or_mod

    class _Resp:
        status_code = 200
        text = ""

        def json(self):
            return {
                "choices": [
                    {"message": {"content": '{"title":"ok"}'}},
                ]
            }

    class _Client:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, url, headers=None, json=None):
            assert url.endswith("/chat/completions")
            assert headers["Authorization"] == "Bearer test-key"
            assert json["model"] == "anthropic/claude-sonnet-4"
            assert json["response_format"]["type"] == "json_object"
            return _Resp()

    monkeypatch.setattr(or_mod.httpx, "AsyncClient", _Client)
    text = await or_mod.complete_json(
        "return json",
        model="anthropic/claude-sonnet-4",
        api_key="test-key",
        base_url="https://openrouter.ai/api/v1",
    )
    assert text == '{"title":"ok"}'


@pytest.mark.asyncio
async def test_openrouter_array_root_uses_wrapped_structured_output(monkeypatch):
    from app.llm import openrouter as or_mod

    calls: list[dict] = []

    class _Resp:
        status_code = 200
        text = ""

        def json(self):
            return {
                "choices": [
                    {"message": {"content": '{"items":[{"title":"ok"}]}'}},
                ]
            }

    class _Client:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, url, headers=None, json=None):
            calls.append(json or {})
            return _Resp()

    monkeypatch.setattr(or_mod.httpx, "AsyncClient", _Client)
    text = await or_mod.complete_json(
        "return an array",
        model="google/gemini-flash",
        api_key="test-key",
        json_root="array",
    )

    assert text.startswith("[")
    assert calls[0]["response_format"]["type"] == "json_object"
    assert '"items"' in calls[0]["messages"][0]["content"]


@pytest.mark.asyncio
async def test_openrouter_retries_without_response_format(monkeypatch):
    from app.llm import openrouter as or_mod

    calls: list[dict] = []

    class _Bad:
        status_code = 400
        text = "response_format not supported"

        def json(self):
            return {}

    class _Good:
        status_code = 200
        text = ""

        def json(self):
            return {"choices": [{"message": {"content": '{"a":1}'}}]}

    class _Client:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, url, headers=None, json=None):
            calls.append(json or {})
            if "response_format" in (json or {}):
                return _Bad()
            return _Good()

    monkeypatch.setattr(or_mod.httpx, "AsyncClient", _Client)
    text = await or_mod.complete_json(
        "x",
        model="meta-llama/llama-4-maverick",
        api_key="k",
    )
    assert text == '{"a":1}'
    assert len(calls) == 2
    assert "response_format" in calls[0]
    assert "response_format" not in calls[1]


def test_openrouter_gemini_request_omits_temperature_and_caps_thinking():
    from app.llm.openrouter import _request_bodies, is_thinking_gemini_model

    assert is_thinking_gemini_model("google/gemini-3.7-flash") is True
    assert is_thinking_gemini_model("google/gemini-2.5-pro") is True
    assert is_thinking_gemini_model("anthropic/claude-sonnet-4") is False

    primary, retry = _request_bodies(
        model_id="google/gemini-3.7-flash",
        prompt="give themes",
        system="Return ONLY valid JSON.",
        temperature=0.3,
        max_tokens=1800,
    )
    assert primary["model"] == "google/gemini-3.7-flash"
    assert "temperature" not in primary
    assert primary["max_tokens"] == 4096
    assert primary["reasoning"] == {"effort": "low", "exclude": True}
    assert primary["messages"][0]["role"] == "user"
    assert "Return ONLY valid JSON" in primary["messages"][0]["content"]
    assert "response_format" in primary
    assert "response_format" not in retry
    assert retry["reasoning"] == {"effort": "low", "exclude": True}


@pytest.mark.asyncio
async def test_openrouter_gemini_reads_reasoning_when_content_empty(monkeypatch):
    from app.llm import openrouter as or_mod

    class _Resp:
        status_code = 200
        text = ""

        def json(self):
            return {
                "choices": [
                    {
                        "message": {
                            "content": "",
                            "reasoning": '{"title":"from-thinking"}',
                        }
                    }
                ]
            }

    class _Client:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, url, headers=None, json=None):
            return _Resp()

    monkeypatch.setattr(or_mod.httpx, "AsyncClient", _Client)
    text = await or_mod.complete_json(
        "x",
        model="google/gemini-3.7-flash",
        api_key="k",
    )
    assert text == '{"title":"from-thinking"}'


@pytest.mark.asyncio
async def test_openrouter_gemini_retries_generic_400(monkeypatch):
    from app.llm import openrouter as or_mod

    calls: list[dict] = []

    class _Bad:
        status_code = 400
        text = "Provider returned error"

        def json(self):
            return {}

    class _Good:
        status_code = 200
        text = ""

        def json(self):
            return {"choices": [{"message": {"content": '{"ok":true}'}}]}

    class _Client:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, url, headers=None, json=None):
            calls.append(json or {})
            if "response_format" in (json or {}):
                return _Bad()
            return _Good()

    monkeypatch.setattr(or_mod.httpx, "AsyncClient", _Client)
    text = await or_mod.complete_json(
        "x",
        model="google/gemini-2.5-flash",
        api_key="k",
    )
    assert text == '{"ok":true}'
    assert len(calls) == 2
    assert "temperature" not in calls[0]
    assert calls[0]["reasoning"]["effort"] == "low"
