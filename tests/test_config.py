"""Settings precedence (env > stored > default) and provider wiring."""

from __future__ import annotations

from toolspotter.config import Settings
from toolspotter.engine.pipeline import RoutingEngine
from toolspotter.store import load_secret, save_secret, save_stored_config


def test_defaults_are_offline():
    settings = Settings.from_env()
    assert settings.llm_provider == "none"
    assert settings.embedding_provider == "local"
    assert settings.llm_model == ""


def test_stored_config_is_used_when_no_env():
    save_stored_config({"llm_provider": "gemini", "llm_model": "stored-model"})
    settings = Settings.from_env()
    assert settings.llm_provider == "gemini"
    assert settings.llm_model == "stored-model"


def test_environment_beats_stored_config(monkeypatch):
    save_stored_config({"llm_provider": "gemini", "llm_model": "stored-model"})
    monkeypatch.setenv("TOOLSPOTTER_LLM_PROVIDER", "none")
    monkeypatch.setenv("TOOLSPOTTER_LLM_MODEL", "env-model")
    settings = Settings.from_env()
    assert settings.llm_provider == "none"
    assert settings.llm_model == "env-model"


def test_thresholds_can_be_set_from_the_environment(monkeypatch):
    monkeypatch.setenv("TOOLSPOTTER_FLOOR", "0.25")
    monkeypatch.setenv("TOOLSPOTTER_HYDE_MAX", "0.5")
    monkeypatch.setenv("TOOLSPOTTER_ARBITRATE_MIN", "0.6")
    confidence = Settings.from_env().confidence
    assert (confidence.out_of_scope_floor, confidence.hyde_max,
            confidence.arbitration_min_fused) == (0.25, 0.5, 0.6)


def test_bad_numeric_env_falls_back_to_default(monkeypatch):
    monkeypatch.setenv("TOOLSPOTTER_FLOOR", "not-a-number")
    assert Settings.from_env().confidence.out_of_scope_floor == 0.18


def test_configured_model_reaches_the_chat_client(catalog, monkeypatch):
    seen: dict = {}

    class RecordingChat:
        def __init__(self, model_id=None):
            seen["model_id"] = model_id

        def complete(self, system, user, max_tokens=700):
            return "{}"

    monkeypatch.setattr("toolspotter.providers.gemini.GeminiChat", RecordingChat)
    RoutingEngine(catalog, settings=Settings(llm_provider="gemini", llm_model="my-model"))
    assert seen["model_id"] == "my-model"


def test_unset_model_lets_the_provider_choose_its_default(catalog, monkeypatch):
    seen: dict = {}

    class RecordingChat:
        def __init__(self, model_id=None):
            seen["model_id"] = model_id

        def complete(self, system, user, max_tokens=700):
            return "{}"

    monkeypatch.setattr("toolspotter.providers.gemini.GeminiChat", RecordingChat)
    RoutingEngine(catalog, settings=Settings(llm_provider="gemini"))
    assert seen["model_id"] is None


def test_missing_credentials_degrade_to_the_local_planner(catalog):
    engine = RoutingEngine(catalog, settings=Settings(llm_provider="gemini"))
    assert type(engine.planner).__name__ == "LocalPlanner"
    assert engine.route("Check the health status of the production server").selected_tools


def test_unknown_provider_stays_offline_and_says_so(catalog, caplog):
    engine = RoutingEngine(catalog, settings=Settings(llm_provider="not-a-provider"))
    assert type(engine.planner).__name__ == "LocalPlanner"
    assert "unknown llm provider" in caplog.text


def test_secrets_round_trip_through_the_fallback_file(sandbox):
    where = save_secret("gemini_key", "abc123")
    assert where in ("keyring", "file(0600)")
    assert load_secret("gemini_key") == "abc123"
