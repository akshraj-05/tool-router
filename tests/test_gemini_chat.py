"""The Gemini chat client: default model, thinking and cut-off replies."""

from __future__ import annotations

import pytest

from tool_router.domain.errors import ProviderError
from tool_router.providers import gemini


@pytest.fixture
def sent(monkeypatch):
    """Capture the request and answer with whatever ``sent.reply`` holds."""

    class Sent:
        reply = {"candidates": [{"content": {"parts": [{"text": "OK"}]}, "finishReason": "STOP"}]}
        url = payload = None

    def post(url, payload):
        Sent.url, Sent.payload = url, payload
        return Sent.reply

    monkeypatch.setenv("GEMINI_API_KEY", "fake")
    monkeypatch.setattr(gemini, "_post", post)
    return Sent


def test_the_default_model(sent):
    gemini.GeminiChat().complete("system", "user")

    assert "/gemini-3.5-flash-lite:generateContent" in sent.url
    assert "thinkingConfig" not in sent.payload["generationConfig"]


@pytest.mark.parametrize("model", ["gemini-2.5-flash", "gemini-2.5-flash-lite"])
def test_thinking_is_switched_off_where_it_can_be(sent, model):
    gemini.GeminiChat(model).complete("system", "user")

    assert sent.payload["generationConfig"]["thinkingConfig"] == {"thinkingBudget": 0}


def test_a_reply_cut_off_by_thinking_is_an_error(sent):
    sent.reply = {"candidates": [{"content": {"parts": [{"text": "`get"}]}, "finishReason": "MAX_TOKENS"}],
                  "usageMetadata": {"thoughtsTokenCount": 150}}

    with pytest.raises(ProviderError, match="thinking"):
        gemini.GeminiChat("some-thinking-model").complete("system", "user")


def test_a_long_reply_without_thinking_is_still_returned(sent):
    sent.reply = {"candidates": [{"content": {"parts": [{"text": "long text"}]}, "finishReason": "MAX_TOKENS"}]}

    assert gemini.GeminiChat().complete("system", "user") == "long text"


@pytest.mark.parametrize("reply", [{}, {"candidates": []}, {"candidates": [None]}])
def test_an_unexpected_reply_is_a_provider_error(sent, reply):
    sent.reply = reply

    with pytest.raises(ProviderError):
        gemini.GeminiChat().complete("system", "user")
