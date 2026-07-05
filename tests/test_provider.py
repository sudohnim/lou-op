"""P4 spec: one Provider — usage/cost/retry structural, vendors are config."""

from __future__ import annotations

import httpx
import pytest

from lou_op.adapters.provider_openai import OpenAICompatProvider


def _response(prompt_toks=100, completion_toks=50, content="hi"):
    return {
        "choices": [{"message": {"content": content, "tool_calls": []}}],
        "usage": {
            "prompt_tokens": prompt_toks,
            "completion_tokens": completion_toks,
            "total_tokens": prompt_toks + completion_toks,
        },
    }


@pytest.fixture()
def provider(monkeypatch):
    p = OpenAICompatProvider(
        "https://api.example.com/v1",
        "key",
        "model-x",
        price_in_per_mtok=1.0,
        price_out_per_mtok=2.0,
    )
    calls = {"n": 0, "headers": None}

    def fake_post(url, *, headers, json, timeout):
        calls["n"] += 1
        calls["headers"] = headers

        class R:
            def raise_for_status(self):
                pass

            def json(self):
                return _response()

        return R()

    monkeypatch.setattr(httpx, "post", fake_post)
    p._calls = calls
    return p


class TestAccounting:
    def test_completion_carries_usage_and_cost(self, provider) -> None:
        c = provider.complete([{"role": "user", "content": "q"}])
        assert c.usage.total == 150
        assert c.cost_usd == pytest.approx((100 * 1.0 + 50 * 2.0) / 1e6)

    def test_cumulative_across_calls(self, provider) -> None:
        provider.complete([{"role": "user", "content": "q"}])
        provider.complete([{"role": "user", "content": "q"}])
        assert provider.usage.total == 300
        assert provider.cost_usd == pytest.approx(2 * (100 + 100) / 1e6)

    def test_generate_same_accounted_path(self, provider) -> None:
        assert provider.generate("q") == "hi"
        assert provider.usage.total == 150  # judge/extractor path accounted


class TestVendorAsConfig:
    def test_baseten_auth_scheme(self, provider) -> None:
        provider.auth_scheme = "Api-Key"
        provider.complete([{"role": "user", "content": "q"}])
        assert provider._calls["headers"]["Authorization"].startswith("Api-Key ")

    def test_insecure_base_url_rejected(self) -> None:
        with pytest.raises(ValueError, match="insecure base_url"):
            OpenAICompatProvider("http://api.example.com/v1", "k", "m")


class TestRetry:
    def test_retries_then_succeeds(self, monkeypatch) -> None:
        p = OpenAICompatProvider("https://x.example/v1", "k", "m", retries=3)
        attempts = {"n": 0}

        def flaky(url, *, headers, json, timeout):
            attempts["n"] += 1
            if attempts["n"] < 3:
                raise httpx.ConnectError("boom")

            class R:
                def raise_for_status(self):
                    pass

                def json(self):
                    return _response()

            return R()

        monkeypatch.setattr(httpx, "post", flaky)
        monkeypatch.setattr(
            "lou_op.adapters.provider_openai.time.sleep", lambda s: None
        )
        assert p.complete([{"role": "user", "content": "q"}]).text == "hi"
        assert attempts["n"] == 3

    def test_exhausted_retries_raise(self, monkeypatch) -> None:
        p = OpenAICompatProvider("https://x.example/v1", "k", "m", retries=2)

        def dead(url, *, headers, json, timeout):
            raise httpx.ConnectError("down")

        monkeypatch.setattr(httpx, "post", dead)
        monkeypatch.setattr(
            "lou_op.adapters.provider_openai.time.sleep", lambda s: None
        )
        with pytest.raises(httpx.ConnectError):
            p.complete([{"role": "user", "content": "q"}])
