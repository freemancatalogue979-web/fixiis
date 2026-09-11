"""
server_settings.py
==================

Runtime-mutable server settings, persisted to ``data/server_settings.json``.

These flags are the *live* knobs an admin can flip from the Admin panel.
They take effect immediately (no restart), and changes are broadcast over
the admin WebSocket so every connected admin UI re-renders.

What lives here (and what does NOT)
-----------------------------------
* Single source of truth for runtime flags.  Other modules
  (``dom_capture``, ``api``, ``session_manager``, ...) call
  :func:`get_settings` to read the current value and :func:`set_settings`
  or :func:`update_setting` to mutate it.
* Flags that need restart-to-take-effect still live in
  ``config.UltraConfig`` (host/port/proxy creds/etc).  Don't move those
  here.

Defaults
--------
See :data:`DEFAULT_SETTINGS`.  The defaults are designed to be safe: LPV
mode is OFF, both capture backends are ON, default capture ordering is
"extension first, library as fallback".
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Settings dataclass
# ---------------------------------------------------------------------------

# Where the JSON file lives.  ``data/`` already exists; if not, we create it.
_BASE_DIR = Path(__file__).resolve().parent
_SETTINGS_PATH = Path(
    os.environ.get("SERVER_SETTINGS_PATH", str(_BASE_DIR / "data" / "server_settings.json"))
)


@dataclass
class ServerSettings:
    """Runtime-mutable server settings.

    All fields default to safe values.  ``to_dict`` / ``from_dict`` round-trip
    through JSON; missing keys keep their default so old config files keep
    working after we add new fields.
    """

    # When True, NEW client connections do NOT create a Playwright/Chrome
    # session.  The client only ever receives LPV pushes.  This is the
    # "boot users into LPV mode without manually" flag.
    lpv_only_mode: bool = False

    # Capture engine toggles.  Both default to ON.  The runtime ordering is
    # always: try extension first, fall back to library.  If both are OFF
    # the capture path returns None and the caller handles the missing
    # document.
    enable_extension_capture: bool = True
    enable_live_library: bool = True

    # Path / filename of the LPV default landing page.  The page is served
    # to clients immediately on connect when in LPV-only mode AND no page
    # has been pushed yet.  It must be inside the LPV pages dir.
    lpv_default_page: str = "_default.html"

    # When True, the LPV default page shows the branded orbit spinner that
    # matches the existing transition overlay look.
    lpv_default_uses_brand_spinner: bool = True

    # -------------------------------------------------------------------------
    # Auto-workflow
    # -------------------------------------------------------------------------
    # When ``auto_workflow_id`` is set AND we're in LPV-only mode, every
    # new client that connects gets this workflow auto-played.  The
    # workflow fires once per connect (reconnects replay).  Empty string
    # disables auto-play.
    auto_workflow_id: str = ""

    # Last-modified timestamp, set automatically by set_settings().
    updated_at: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ServerSettings":
        # Build with defaults, then overlay the file's values.  Unknown keys
        # are ignored (forward compat: future versions can add new fields and
        # old config files still load).
        defaults = cls()
        out: Dict[str, Any] = defaults.to_dict()
        if isinstance(data, dict):
            for k, v in data.items():
                if k in out:
                    out[k] = v
        return cls(**out)


DEFAULT_SETTINGS = ServerSettings()


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

def _load_from_disk() -> ServerSettings:
    """Read settings from disk, returning defaults on any failure."""
    try:
        if not _SETTINGS_PATH.exists():
            return ServerSettings()
        with open(_SETTINGS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        return ServerSettings.from_dict(data)
    except Exception as exc:
        logger.warning(
            "[server_settings] failed to load %s: %s; using defaults",
            _SETTINGS_PATH, exc,
        )
        return ServerSettings()


def _save_to_disk(settings: ServerSettings) -> None:
    """Atomically write settings to disk.  Best-effort: errors are logged."""
    try:
        _SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = _SETTINGS_PATH.with_suffix(_SETTINGS_PATH.suffix + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(settings.to_dict(), f, indent=2, sort_keys=True)
        os.replace(tmp, _SETTINGS_PATH)
    except Exception as exc:
        logger.error(
            "[server_settings] failed to persist settings to %s: %s",
            _SETTINGS_PATH, exc,
        )


# ---------------------------------------------------------------------------
# Module-level state
# ---------------------------------------------------------------------------

_settings: ServerSettings = _load_from_disk()
_settings_lock = asyncio.Lock()
# Subscribers get notified after every successful mutation.  Used by the
# WebSocket layer in api.py to broadcast to admin panels.
_subscribers: List[Callable[[Dict[str, Any]], Awaitable[None]]] = []


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_settings() -> ServerSettings:
    """Return a snapshot of the current settings.  Callers MUST NOT mutate
    the returned object; use :func:`update_setting` instead."""
    return _settings


def get_settings_dict() -> Dict[str, Any]:
    """Convenience: get a plain dict (JSON-safe) for the admin REST endpoint."""
    return _settings.to_dict()


def reload_from_disk() -> ServerSettings:
    """Re-read the JSON file.  Mostly useful for tests; normal operation
    never calls this."""
    global _settings
    _settings = _load_from_disk()
    return _settings


def subscribe(callback: Callable[[Dict[str, Any]], Awaitable[None]]) -> None:
    """Register a coroutine to be called after every successful settings
    mutation.  The coroutine receives the new settings as a dict.

    The subscriber is responsible for handling its own errors; the broadcaster
    does not retry.
    """
    if callback not in _subscribers:
        _subscribers.append(callback)


def unsubscribe(callback: Callable[[Dict[str, Any]], Awaitable[None]]) -> None:
    if callback in _subscribers:
        _subscribers.remove(callback)


async def _notify_subscribers(payload: Dict[str, Any]) -> None:
    """Fire-and-forget fan-out.  Each subscriber runs independently; one
    failing subscriber does not block the others."""
    for cb in list(_subscribers):
        try:
            await cb(payload)
        except Exception as exc:
            logger.warning(
                "[server_settings] subscriber %r raised: %s",
                getattr(cb, "__name__", cb), exc,
            )


async def update_setting(key: str, value: Any) -> ServerSettings:
    """Update a single key, persist, and broadcast.

    Unknown keys are silently ignored.  Returns the new settings.
    """
    global _settings
    if key not in DEFAULT_SETTINGS.to_dict():
        logger.warning("[server_settings] ignoring unknown key: %s", key)
        return _settings

    async with _settings_lock:
        current = _settings.to_dict()
        current[key] = value
        current["updated_at"] = time.time()
        _settings = ServerSettings(**current)
        _save_to_disk(_settings)

    await _notify_subscribers({
        "type": "settings_update",
        "key": key,
        "value": value,
        "settings": _settings.to_dict(),
    })
    return _settings


async def set_settings(patch: Dict[str, Any]) -> ServerSettings:
    """Apply a partial patch.  Only known keys are applied; unknown keys
    are dropped.  Returns the new settings."""
    global _settings
    if not isinstance(patch, dict):
        return _settings

    async with _settings_lock:
        current = _settings.to_dict()
        changed = False
        for k, v in patch.items():
            if k in DEFAULT_SETTINGS.to_dict() and k != "updated_at":
                if current[k] != v:
                    current[k] = v
                    changed = True
        if not changed:
            return _settings
        current["updated_at"] = time.time()
        _settings = ServerSettings(**current)
        _save_to_disk(_settings)

    await _notify_subscribers({
        "type": "settings_update",
        "patch": patch,
        "settings": _settings.to_dict(),
    })
    return _settings


def settings_file_path() -> str:
    """For diagnostics; admin UI may want to display this."""
    return str(_SETTINGS_PATH)
