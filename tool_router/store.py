"""Vector cache, settings and secrets.

- **Cache** (``JsonCache``): ``{content hash: value}`` bound to a model stamp. It is
  kept in the state folder (see ``state.py``) or, without one, only in memory. A
  value is reused only when both the tool's text hash and the model stamp match;
  the local TF-IDF stamp includes a corpus hash, so a catalog change invalidates
  its vectors.
- **Settings and secrets** live in the per-user config folder, never in a project:
      config.json   non-secret settings written by `tool-router init` / /setup
      secrets.json  0600-permission fallback when the OS keyring is unavailable
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any, Optional

from platformdirs import user_config_dir

from tool_router.log import get_logger
from tool_router.state import write_atomic

logger = get_logger("store")

_APP, _AUTHOR = "tool_router", "tool_router"
_SECRET_SERVICE = "tool-router"


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:24]


def config_dir() -> Path:
    return Path(user_config_dir(_APP, _AUTHOR))


class JsonCache:
    """A small persisted {key: value} cache bound to a model stamp."""

    def __init__(self, path: Optional[Path], model_stamp: str, rebuild: bool = False) -> None:
        """``path=None`` keeps the cache in memory only."""
        self._path = path
        self._stamp = model_stamp
        self._data: dict[str, Any] = {}
        if path is None:
            return
        if rebuild:
            logger.info("cache rebuild requested — starting fresh: %s", path.name)
            return
        try:
            if path.exists():
                payload = json.loads(path.read_text())
                if payload.get("model_stamp") == model_stamp:
                    self._data = payload.get("entries", {})
                else:  # model changed -> stored vectors are incompatible
                    logger.info(
                        "model stamp changed (%s -> %s) — invalidating %s",
                        payload.get("model_stamp"), model_stamp, path.name,
                    )
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("could not read cache %s (%s) — starting fresh", path, exc)

    def get(self, key: str) -> Any:
        return self._data.get(key)

    def put(self, key: str, value: Any) -> None:
        self._data[key] = value

    def keep(self, keys: set[str]) -> bool:
        """Drop every other entry. Returns True if anything was dropped."""
        stale = self._data.keys() - keys
        for key in stale:
            del self._data[key]
        return bool(stale)

    def save(self) -> None:
        if self._path is None:
            return
        try:
            self._path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            write_atomic(self._path, json.dumps({"model_stamp": self._stamp, "entries": self._data}))
        except OSError as exc:
            logger.warning("could not persist cache %s (%s) — continuing in-memory", self._path, exc)

    @property
    def size(self) -> int:
        return len(self._data)


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
