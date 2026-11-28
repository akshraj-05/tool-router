"""Command-line interface."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from toolspotter import __version__
from toolspotter.cli import main

DATA = Path(__file__).parent / "data"


def test_version(capsys):
    assert main(["version"]) == 0
    assert capsys.readouterr().out.strip() == __version__


def test_mcp_requires_a_catalog(capsys):
    with pytest.raises(SystemExit) as exit_info:
        main(["mcp"])
    assert exit_info.value.code == 2
    assert "--catalog" in capsys.readouterr().err


def test_validate_reports_accept_and_reject(tmp_path, capsys):
    tools = json.loads((DATA / "catalog.json").read_text())["tools"][:1]
    tools.append({"id": "Bad", "name": "x"})
    path = tmp_path / "tools.json"
    path.write_text(json.dumps(tools))
    assert main(["validate", str(path)]) == 0
    out = capsys.readouterr().out
    assert "ACCEPT  fin.get_revenue_report" in out and "REJECT" in out
    assert "1 accepted, 1 rejected" in out


def test_config_set_show_reset(capsys):
    assert main(["config", "set", "llm_provider", "gemini"]) == 0
    capsys.readouterr()
    main(["config", "show"])
    assert "llm_provider = gemini" in capsys.readouterr().out
    main(["config", "reset"])
    capsys.readouterr()
    main(["config", "show"])
    assert "llm_provider" not in capsys.readouterr().out


def test_init_offers_gemini_and_stores_its_key(monkeypatch):
    from toolspotter.store import load_secret, load_stored_config

    answers = iter(["gemini", "gemini-test-model", "local"])
    monkeypatch.setattr("builtins.input", lambda _prompt="": next(answers))
    monkeypatch.setattr("getpass.getpass", lambda _prompt="": "g-key")
    assert main(["init"]) == 0
    stored = load_stored_config()
    assert stored["llm_provider"] == "gemini" and stored["llm_model"] == "gemini-test-model"
    assert load_secret("gemini_key") == "g-key"
