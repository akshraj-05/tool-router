"""Settings, secrets and the content hash.

Both live in the per-user config folder, never in a project:

      config.json   non-secret settings written by `tool-router init` / /setup
      secrets.json  0600-permission fallback when the OS keyring is unavailable

Tools, vectors and request history are in the state folder instead: see ``database.py``.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any, Optional

from platformdirs import user_config_dir

from tool_router.log import get_logger

logger = get_logger("store")

_APP, _AUTHOR = "tool_router", "tool_router"
_SECRET_SERVICE = "tool-router"


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:24]


def config_dir() -> Path:
    return Path(user_config_dir(_APP, _AUTHOR))


# --------------------------- config + secrets -------------------------------

def load_stored_config() -> dict[str, Any]:
    path = config_dir() / "config.json"
    try:
        return json.loads(path.read_text()) if path.exists() else {}
    except (OSError, json.JSONDecodeError):
        return {}


def save_stored_config(values: dict[str, Any]) -> Path:
    path = config_dir() / "config.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    merged = {**load_stored_config(), **values}
    path.write_text(json.dumps(merged, indent=1) + "\n")
    return path


def save_secret(name: str, value: str) -> str:
    """Store a secret in the OS keyring, else a 0600 fallback file. Never logged."""
    try:
        import keyring
        keyring.set_password(_SECRET_SERVICE, name, value)
        return "keyring"
    except Exception:
        path = config_dir() / "secrets.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        data = {}
        if path.exists():
            try:
                data = json.loads(path.read_text())
            except (OSError, json.JSONDecodeError):
                data = {}
        data[name] = value
        path.write_text(json.dumps(data))
        os.chmod(path, 0o600)
        return "file(0600)"


def load_secret(name: str) -> Optional[str]:
    """Env var wins; then keyring; then the 0600 fallback file."""
    env_map = {"gemini_key": "GEMINI_API_KEY"}
    env_val = os.environ.get(env_map.get(name, name.upper()))
    if env_val:
        return env_val
    try:
        import keyring
        val = keyring.get_password(_SECRET_SERVICE, name)
        if val:
            return val
    except Exception:
        pass
    path = config_dir() / "secrets.json"
    try:
        if path.exists():
            return json.loads(path.read_text()).get(name)
    except (OSError, json.JSONDecodeError):
        pass
    return None
