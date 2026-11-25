"""Persistence layer (storage.md): OS-agnostic dirs, project isolation, caches.

Layout (never a hardcoded /opt-style path — see storage.md):

    <data_dir>/tool_router/projects/<project-id>/
        embeddings.json    per-tool vectors keyed by content hash + model stamp
        enrichment.json    one-time LLM enrichment results keyed by content hash

- ``data_dir`` = $TOOL_ROUTER_DATA_DIR override, else the platformdirs per-OS
  user-data location (Linux ~/.local/share, macOS ~/Library/..., Windows AppData).
- ``project id`` = auto via a ``.tool_router`` marker file in the working
  directory (stable across renames/moves); falls back to a hash of the cwd
  when the directory isn't writable. Two projects can never collide.
- **Content-hash cache**: a vector is reused only when BOTH the tool's text
  hash AND the model stamp match. For corpus-dependent models (local TF-IDF)
  the stamp includes the corpus hash, which correctly invalidates the whole
  cache when the catalog changes; for hosted models (Gemini) the stamp is the
  model id, so mutations re-embed only changed tools.
- ``rebuild=True`` (or $TOOL_ROUTER_REBUILD=1, or ``serve --rebuild``) wipes
  and re-derives — the escape hatch.

Config + secrets live in the platformdirs *config* dir:
    config.json   non-secret settings written by `tool-router init` / /setup
    secrets.json  0600-permission fallback when the OS keyring is unavailable
"""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from pathlib import Path
from typing import Any, Optional

from platformdirs import user_config_dir, user_data_dir

from tool_router.log import get_logger

logger = get_logger("store")

_APP, _AUTHOR = "tool_router", "tool_router"
_MARKER = ".tool_router"
_SECRET_SERVICE = "tool-router"


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:24]


def data_dir() -> Path:
    override = os.environ.get("TOOL_ROUTER_DATA_DIR")
    return Path(override) if override else Path(user_data_dir(_APP, _AUTHOR))


def config_dir() -> Path:
    return Path(user_config_dir(_APP, _AUTHOR))


def resolve_project_id(project_dir: Optional[Path] = None) -> str:
    """Stable per-project id via a marker file (auto multi-project isolation)."""
    root = Path(project_dir or Path.cwd())
    marker = root / _MARKER
    try:
        if marker.exists():
            pid = json.loads(marker.read_text()).get("project_id")
            if isinstance(pid, str) and pid:
                return pid
        pid = uuid.uuid4().hex[:12]
        marker.write_text(json.dumps({"project_id": pid}) + "\n")
        logger.info("created project marker %s (add %s to .gitignore)", marker, _MARKER)
        return pid
    except OSError:
        # Read-only location: fall back to a path hash (still isolated, just
        # not rename-stable — documented in storage.md).
        return content_hash(str(root.resolve()))[:12]


class JsonCache:
    """A small persisted {key: value} cache bound to a model stamp."""

    def __init__(self, path: Path, model_stamp: str, rebuild: bool = False) -> None:
        self._path = path
        self._stamp = model_stamp
        self._data: dict[str, Any] = {}
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

    def save(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(json.dumps(
                {"model_stamp": self._stamp, "entries": self._data}
            ))
        except OSError as exc:
            logger.warning("could not persist cache %s (%s) — continuing in-memory", self._path, exc)

    @property
    def size(self) -> int:
        return len(self._data)


def project_cache(name: str, model_stamp: str, rebuild: bool = False) -> JsonCache:
    """Open a named cache under the current project's data directory."""
    pid = resolve_project_id()
    return JsonCache(data_dir() / "projects" / pid / f"{name}.json", model_stamp, rebuild)


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
