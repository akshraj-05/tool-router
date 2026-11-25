"""Shared fixtures.

Every test runs in an isolated sandbox: its own working directory (the store
writes a project marker into the cwd), its own data and config directories, and
no TOOL_ROUTER_* / provider key variables leaking in from the developer's shell.
"""

from __future__ import annotations

import copy
import json
import os
from pathlib import Path

import pytest

from tool_router.config import Settings
from tool_router.engine.pipeline import RoutingEngine

DATA = Path(__file__).parent / "data"

_SCRUBBED_ENV = ("GEMINI_API_KEY",)


@pytest.fixture(autouse=True)
def sandbox(tmp_path, monkeypatch):
    for name in list(os.environ):
        if name.startswith("TOOL_ROUTER_") or name in _SCRUBBED_ENV:
            monkeypatch.delenv(name, raising=False)
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.chdir(work)
    monkeypatch.setenv("TOOL_ROUTER_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    return tmp_path


@pytest.fixture
def catalog() -> dict:
    """A fresh copy of the sample catalog (tests may mutate it)."""
    return copy.deepcopy(json.loads((DATA / "catalog.json").read_text()))


@pytest.fixture
def engine(catalog) -> RoutingEngine:
    """Offline engine: local TF-IDF, no LLM."""
    return RoutingEngine(catalog, settings=Settings())


class FakeChat:
    """Stand-in for a hosted LLM: returns scripted replies and records prompts."""

    def __init__(self, *replies: str | Exception) -> None:
        self._replies = list(replies)
        self.calls: list[tuple[str, str]] = []

    def complete(self, system: str, user: str, max_tokens: int = 700) -> str:
        self.calls.append((system, user))
        reply = self._replies.pop(0) if len(self._replies) > 1 else self._replies[0]
        if isinstance(reply, Exception):
            raise reply
        return reply


@pytest.fixture
def fake_chat():
    return FakeChat
