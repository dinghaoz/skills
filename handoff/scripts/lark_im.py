#!/usr/bin/env python3
"""Lark IM API client for handoff messaging."""

import base64
import hashlib
import json
import os
import re
import socket
import sqlite3
import ssl
import struct
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

BASE_URL = "https://open.larksuite.com/open-apis"

HANDOFF_HOME = os.path.expanduser("~/.handoff")
_CHAT_ID_RE = re.compile(r"^[A-Za-z0-9._:@-]+$")
_CHAT_ID_MAX_LEN = 128


def is_valid_chat_id(chat_id):
    """Return True if chat_id contains only URL-safe characters.

    Intentionally loose — must work across Lark, Slack, and future platforms.
    Rejects characters that could cause URL path injection (/, ?, #, &, spaces).
    """
    if not chat_id or not isinstance(chat_id, str):
        return False
    if len(chat_id) > _CHAT_ID_MAX_LEN:
        return False
    return bool(_CHAT_ID_RE.match(chat_id))


def default_config_file():
    return os.path.join(HANDOFF_HOME, "config.json")


CONFIG_FILE = default_config_file()

from lark_auth import LarkAuth  # noqa: E402

_auth = LarkAuth(CONFIG_FILE)


def _require_project_dir():
    """Return HANDOFF_PROJECT_DIR (or CLAUDE_PROJECT_DIR fallback) or raise.

    Hooks (PostToolUse, Notification, etc.) run as subprocesses and may only
    have CLAUDE_PROJECT_DIR in their env, not HANDOFF_PROJECT_DIR.  The main
    process and Bash tool calls get HANDOFF_PROJECT_DIR via the session env
    file.  We try both so the DB path resolves in either context.
    """
    project_dir = os.environ.get("HANDOFF_PROJECT_DIR") or os.environ.get(
        "CLAUDE_PROJECT_DIR"
    )
    if not project_dir:
        raise RuntimeError(
            "HANDOFF_PROJECT_DIR is not set. "
            "Ensure the SessionStart hook has run to persist it."
        )
    return project_dir


def _get_machine_name():
    """Get the machine name, preferring macOS ComputerName over hostname.

    On macOS, socket.gethostname() can return the IP address (e.g. 192.168.0.114)
    when configd is out of sync with System Settings. Read the ComputerName from
    the SystemConfiguration plist as the primary source.
    """
    if sys.platform == "darwin":
        try:
            import plistlib
            plist_path = "/Library/Preferences/SystemConfiguration/preferences.plist"
            with open(plist_path, "rb") as f:
                data = plistlib.load(f)
            name = data.get("System", {}).get("System", {}).get("ComputerName")
            if name:
                return name
        except Exception:
            pass
    return socket.gethostname().split(".")[0]


def get_workspace_id():
    """Compute the workspace ID from machine name + project directory.

    Identifies the physical code location (machine + folder path).
    Used as a tag in Lark group descriptions to associate groups with projects.
    """
    project_dir = _require_project_dir()
    machine = _get_machine_name()
    folder = project_dir.replace("/", "-").strip("-")
    return f"{machine}-{folder}"


def get_worktree_name():
    """Get worktree name from git toplevel or branch, falling back to folder name."""
    import subprocess

    cwd = os.environ.get("HANDOFF_PROJECT_DIR") or os.getcwd()
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            cwd=cwd,
            timeout=5,
        )
        if result.returncode == 0:
            name = os.path.basename(result.stdout.strip())
            if name:
                return name
    except Exception:
        pass
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True,
            text=True,
            cwd=cwd,
            timeout=5,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except Exception:
        pass
    return os.path.basename(cwd)


# ---------------------------------------------------------------------------
# SQLite database — single file stores handoff state and message history
# ---------------------------------------------------------------------------


def _db_path():
    """Return the path to the handoff SQLite database.

    Uses ~/.handoff/projects/<project>/handoff-data.db where <project>
    is derived from the working directory.
    """
    project_dir = _require_project_dir()
    project_name = project_dir.replace("/", "-")
    return os.path.join(
        os.path.join(HANDOFF_HOME, "projects"),
        project_name,
        "handoff-data.db",
    )


_db_initialized = set()  # tracks which DB files have been schema-checked


def handoff_tmp_dir():
    """Base temporary directory for handoff runtime artifacts."""
    custom = os.environ.get("HANDOFF_TMP_DIR", "").strip()
    if custom:
        return custom
    return "/tmp/handoff"


def _cleanup_old_downloads(max_age_hours=24):
    """Remove downloaded files older than max_age_hours to prevent accumulation.

    Called automatically at module import time to clean up handoff-images/
    and handoff-files/ directories. Non-critical — errors are ignored.
    """
    try:
        base = handoff_tmp_dir()
        cutoff = time.time() - (max_age_hours * 3600)
        for subdir in ("handoff-images", "handoff-files"):
            dir_path = os.path.join(base, subdir)
            if not os.path.isdir(dir_path):
                continue
            for entry in os.scandir(dir_path):
                try:
                    if entry.is_file() and entry.stat().st_mtime < cutoff:
                        os.unlink(entry.path)
                except Exception:
                    pass  # Ignore cleanup errors
    except Exception:
        pass  # Non-critical cleanup


# Run cleanup at module load to prevent unbounded accumulation
_cleanup_old_downloads()


_SESSIONS_COLS = {
    "session_id",
    "chat_id",
    "session_tool",
    "session_model",
    "last_checked",
    "activated_at",
    "operator_open_id",
    "bot_open_id",
    "sidecar_mode",
    "guests",
}
_CHAT_PREFS_COLS = {
    "chat_id",
    "message_filter",
}
_WORKING_STATE_COLS = {
    "session_id",
    "message_id",
    "created_at",
}
_MESSAGES_COLS = {
    "message_id",
    "chat_id",
    "direction",
    "source_message_id",
    "message_time",
    "text",
    "title",
    "sent_at",
}


def _check_schema(conn, table, expected_cols):
    """Check if a table has the expected columns. Drop and recreate if not.

    Schema has been stable since early 2025. Rather than migrating data from
    ancient schemas, just drop and recreate — sessions are transient and
    message history is non-critical.
    """
    info = conn.execute(f"PRAGMA table_info({table})").fetchall()
    if not info:
        return  # Table doesn't exist yet, will be created by caller
    actual_cols = {row[1] for row in info}
    if expected_cols <= actual_cols:
        return  # All expected columns present
    conn.execute(f"DROP TABLE {table}")
    conn.execute(f"DROP TABLE IF EXISTS {table}_new")


def _get_db():
    """Open (and auto-create) the handoff database. Returns a connection."""
    db_path = _db_path()
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=5)
    if db_path not in _db_initialized:
        conn.execute("PRAGMA journal_mode=WAL")
        # Drop legacy table from pre-sessions era
        conn.execute("DROP TABLE IF EXISTS state")
        # Check existing tables — drop and recreate if schema is outdated
        _check_schema(conn, "sessions", _SESSIONS_COLS)
        _check_schema(conn, "messages", _MESSAGES_COLS)
        _check_schema(conn, "chat_preferences", _CHAT_PREFS_COLS)
        _check_schema(conn, "working_state", _WORKING_STATE_COLS)
        conn.execute(
            "CREATE TABLE IF NOT EXISTS sessions ("
            "  session_id TEXT NOT NULL PRIMARY KEY,"
            "  chat_id TEXT NOT NULL UNIQUE,"
            "  session_tool TEXT NOT NULL,"
            "  session_model TEXT NOT NULL,"
            "  last_checked INTEGER,"
            "  activated_at INTEGER NOT NULL,"
            "  operator_open_id TEXT NOT NULL DEFAULT '',"
            "  bot_open_id TEXT NOT NULL DEFAULT '',"
            "  sidecar_mode INTEGER NOT NULL DEFAULT 0,"
            "  guests TEXT NOT NULL DEFAULT '[]'"
            ")"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS messages ("
            "  message_id TEXT NOT NULL PRIMARY KEY,"
            "  chat_id TEXT NOT NULL,"
            "  direction TEXT NOT NULL DEFAULT 'sent',"
            "  source_message_id TEXT,"
            "  message_time INTEGER,"
            "  text TEXT,"
            "  title TEXT,"
            "  sent_at INTEGER"
            ")"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS chat_preferences ("
            "  chat_id TEXT NOT NULL PRIMARY KEY,"
            "  message_filter TEXT NOT NULL DEFAULT 'concise'"
            ")"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS working_state ("
            "  session_id TEXT NOT NULL PRIMARY KEY,"
            "  message_id TEXT NOT NULL,"
            "  created_at INTEGER NOT NULL"
            ")"
        )
        conn.commit()
        _db_initialized.add(db_path)
    return conn


def _normalize_session_tool():
    """Resolve session tool name from HANDOFF_SESSION_TOOL env var."""
    tool = os.environ.get("HANDOFF_SESSION_TOOL", "").strip()
    if not tool:
        raise RuntimeError(
            "HANDOFF_SESSION_TOOL is not set. "
            "Ensure the SessionStart hook has run to persist it."
        )
    return tool


def try_claim_chat(session_id, chat_id, session_model,
                   operator_open_id="", bot_open_id="", sidecar_mode=False):
    """Atomically claim a chat for a session.

    Returns (ok, owner_session_id).
    If ok is False, owner_session_id is the session that currently owns chat_id.
    """
    if not session_id or not chat_id:
        return False, None
    tool = _normalize_session_tool()
    conn = _get_db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        owner = conn.execute(
            "SELECT session_id FROM sessions WHERE chat_id = ?",
            (chat_id,),
        ).fetchone()
        if owner and owner[0] != session_id:
            conn.execute("ROLLBACK")
            return False, owner[0]

        conn.execute("DELETE FROM sessions WHERE session_id = ?", (session_id,))
        conn.execute(
            "INSERT INTO sessions"
            " (session_id, chat_id, session_tool, session_model, activated_at,"
            "  operator_open_id, bot_open_id, sidecar_mode, guests)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (session_id, chat_id, tool, session_model, int(time.time()),
             operator_open_id or "", bot_open_id or "",
             1 if sidecar_mode else 0, "[]"),
        )
        conn.execute("COMMIT")
        return True, session_id
    except sqlite3.IntegrityError:
        conn.execute("ROLLBACK")
        owner = conn.execute(
            "SELECT session_id FROM sessions WHERE chat_id = ?",
            (chat_id,),
        ).fetchone()
        return False, owner[0] if owner else None
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except Exception:
            pass
        raise
    finally:
        conn.close()


def register_session(session_id, chat_id, session_model,
                     operator_open_id="", bot_open_id="", sidecar_mode=False):
    """Register a handoff session in the local database."""
    ok, owner = try_claim_chat(
        session_id, chat_id,
        session_model=session_model,
        operator_open_id=operator_open_id,
        bot_open_id=bot_open_id,
        sidecar_mode=sidecar_mode,
    )
    if not ok:
        raise RuntimeError(f"chat_id {chat_id} is already owned by session {owner}")


def get_chat_owner_session(chat_id):
    """Return the session_id currently owning chat_id, or None."""
    if not chat_id:
        return None
    conn = _get_db()
    try:
        row = conn.execute(
            "SELECT session_id FROM sessions WHERE chat_id = ?",
            (chat_id,),
        ).fetchone()
        return row[0] if row else None
    finally:
        conn.close()


def takeover_chat(
    session_id,
    chat_id,
    session_model,
    expected_owner_session_id=None,
    operator_open_id="",
    bot_open_id="",
    sidecar_mode=False,
):
    """Atomically take over a chat for the given session.

    Compare-and-swap semantics:
    - If `expected_owner_session_id` is provided, takeover succeeds only when
      current owner is exactly expected_owner_session_id, or no owner exists
      (old owner ended concurrently).
    - If another owner is present and does not match expected owner, takeover
      fails and returns that owner.

    Returns tuple: (ok, owner_session_id, replaced_owner_session_id)
    """
    if not session_id or not chat_id:
        return False, None, None

    tool = _normalize_session_tool()
    conn = _get_db()
    try:
        conn.execute("BEGIN IMMEDIATE")

        row = conn.execute(
            "SELECT session_id FROM sessions WHERE chat_id = ?",
            (chat_id,),
        ).fetchone()
        current_owner = row[0] if row else None

        if current_owner and current_owner != session_id:
            # Strict compare-and-swap:
            # - expected owner provided -> must match exactly
            # - expected owner omitted -> only allow if no owner is present
            if expected_owner_session_id:
                if current_owner != expected_owner_session_id:
                    conn.execute("ROLLBACK")
                    return False, current_owner, None
            else:
                conn.execute("ROLLBACK")
                return False, current_owner, None

        conn.execute("DELETE FROM sessions WHERE session_id = ?", (session_id,))

        replaced_owner = None
        if current_owner and current_owner != session_id:
            conn.execute("DELETE FROM sessions WHERE session_id = ?", (current_owner,))
            replaced_owner = current_owner

        conn.execute(
            "INSERT INTO sessions"
            " (session_id, chat_id, session_tool, session_model, activated_at,"
            "  operator_open_id, bot_open_id, sidecar_mode, guests)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (session_id, chat_id, tool, session_model, int(time.time()),
             operator_open_id or "", bot_open_id or "",
             1 if sidecar_mode else 0, "[]"),
        )
        conn.execute("COMMIT")
        return True, session_id, replaced_owner
    except sqlite3.IntegrityError:
        conn.execute("ROLLBACK")
        owner = conn.execute(
            "SELECT session_id FROM sessions WHERE chat_id = ?",
            (chat_id,),
        ).fetchone()
        return False, owner[0] if owner else None, None
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except Exception:
            pass
        raise
    finally:
        conn.close()


_STALE_THRESHOLD_SECONDS = 30 * 24 * 60 * 60  # 30 days


def prune_stale_sessions():
    """Delete clearly stale session rows (older than 30 days).

    Uses last_checked (ms) when available, otherwise activated_at (s).
    Returns the number of deleted rows.
    """
    now_s = int(time.time())
    cutoff_s = now_s - _STALE_THRESHOLD_SECONDS
    cutoff_ms = cutoff_s * 1000

    conn = _get_db()
    try:
        cur = conn.execute(
            "DELETE FROM sessions WHERE "
            "((last_checked IS NOT NULL AND last_checked != '' "
            "  AND CAST(last_checked AS INTEGER) < ?) "
            " OR ((last_checked IS NULL OR last_checked = '') "
            "  AND COALESCE(activated_at, 0) < ?))",
            (cutoff_ms, cutoff_s),
        )
        conn.commit()
        return cur.rowcount or 0
    finally:
        conn.close()


def unregister_session(session_id):
    """Remove a handoff session from the local database."""
    conn = _get_db()
    try:
        conn.execute(
            "DELETE FROM sessions WHERE session_id = ?",
            (session_id,),
        )
        conn.commit()
    finally:
        conn.close()


def get_session(session_id):
    """Look up a session by ID. Returns dict or None.

    Dict keys: session_id, chat_id, session_tool, session_model,
    last_checked, activated_at, message_filter, operator_open_id, bot_open_id,
    sidecar_mode, guests.
    """
    conn = _get_db()
    try:
        row = conn.execute(
            "SELECT s.session_id, s.chat_id, s.last_checked, s.activated_at"
            " , s.session_tool, s.session_model"
            " , p.message_filter"
            " , s.operator_open_id"
            " , s.bot_open_id"
            " , s.sidecar_mode"
            " , s.guests"
            " FROM sessions s"
            " LEFT JOIN chat_preferences p ON s.chat_id = p.chat_id"
            " WHERE s.session_id = ?",
            (session_id,),
        ).fetchone()
        if row:
            guests_raw = row[10] or "[]"
            try:
                guests = json.loads(guests_raw)
            except (json.JSONDecodeError, TypeError):
                guests = []
            return {
                "session_id": row[0],
                "chat_id": row[1],
                "last_checked": row[2],
                "activated_at": row[3],
                "session_tool": row[4],
                "session_model": row[5],
                "message_filter": row[6] or "concise",
                "operator_open_id": row[7] or "",
                "bot_open_id": row[8] or "",
                "sidecar_mode": bool(row[9]),
                "guests": guests,
            }
        return None
    finally:
        conn.close()


MESSAGE_FILTER_LEVELS = ("verbose", "important", "concise")


def set_message_filter(chat_id, level):
    """Set the message filter level for a chat group.

    Persists in chat_preferences table (survives session changes).
    level: 'verbose' (all), 'important' (edit+write), 'concise' (none).
    """
    if level not in MESSAGE_FILTER_LEVELS:
        raise ValueError(f"Invalid filter level: {level}")
    conn = _get_db()
    try:
        conn.execute(
            "INSERT INTO chat_preferences (chat_id, message_filter)"
            " VALUES (?, ?)"
            " ON CONFLICT(chat_id) DO UPDATE SET message_filter = ?",
            (chat_id, level, level),
        )
        conn.commit()
    finally:
        conn.close()


def get_guests(session_id):
    """Get the guest whitelist for a session.

    Returns list of dicts: [{"open_id": "ou_xxx", "name": "Alice"}, ...]
    """
    conn = _get_db()
    try:
        row = conn.execute(
            "SELECT guests FROM sessions WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        if not row:
            return []
        try:
            return json.loads(row[0] or "[]")
        except (json.JSONDecodeError, TypeError):
            return []
    finally:
        conn.close()


def set_guests(session_id, guests):
    """Replace the guest whitelist for a session.

    guests: list of dicts [{"open_id": "ou_xxx", "name": "Alice"}, ...]
    """
    conn = _get_db()
    try:
        conn.execute(
            "UPDATE sessions SET guests = ? WHERE session_id = ?",
            (json.dumps(guests, ensure_ascii=False), session_id),
        )
        conn.commit()
    finally:
        conn.close()


def add_guests(session_id, new_guests):
    """Add guests to the whitelist (skip duplicates by open_id).

    new_guests: list of dicts [{"open_id": "ou_xxx", "name": "Alice"}, ...]
    Returns (added, current) — lists of added guests and full current list.
    """
    current = get_guests(session_id)
    existing_ids = {g["open_id"] for g in current}
    added = []
    for g in new_guests:
        if g["open_id"] not in existing_ids:
            current.append(g)
            existing_ids.add(g["open_id"])
            added.append(g)
    if added:
        set_guests(session_id, current)
    return added, current


def remove_guests(session_id, open_ids):
    """Remove guests by open_id from the whitelist.

    open_ids: set or list of open_id strings to remove.
    Returns (removed, current) — lists of removed guests and remaining list.
    """
    current = get_guests(session_id)
    ids_to_remove = set(open_ids)
    removed = [g for g in current if g["open_id"] in ids_to_remove]
    remaining = [g for g in current if g["open_id"] not in ids_to_remove]
    if removed:
        set_guests(session_id, remaining)
    return removed, remaining


def set_working_message(session_id, message_id):
    """Store the "Working..." card message_id for a session."""
    conn = _get_db()
    try:
        conn.execute(
            "INSERT INTO working_state (session_id, message_id, created_at)"
            " VALUES (?, ?, ?)"
            " ON CONFLICT(session_id) DO UPDATE"
            " SET message_id = ?, created_at = ?",
            (session_id, message_id, int(time.time()), message_id, int(time.time())),
        )
        conn.commit()
    finally:
        conn.close()


def get_working_message(session_id):
    """Return the working card message_id for a session, or None."""
    conn = _get_db()
    try:
        row = conn.execute(
            "SELECT message_id FROM working_state WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        return row[0] if row else None
    finally:
        conn.close()


def clear_working_message(session_id):
    """Remove the working card state for a session."""
    conn = _get_db()
    try:
        conn.execute(
            "DELETE FROM working_state WHERE session_id = ?",
            (session_id,),
        )
        conn.commit()
    finally:
        conn.close()


def get_active_sessions():
    """Return all active sessions as a list of dicts."""
    conn = _get_db()
    try:
        rows = conn.execute(
            "SELECT session_id, chat_id, last_checked, activated_at,"
            " session_tool, session_model, operator_open_id, bot_open_id,"
            " sidecar_mode"
            " FROM sessions",
        ).fetchall()
        return [
            {
                "session_id": r[0],
                "chat_id": r[1],
                "last_checked": r[2],
                "activated_at": r[3],
                "session_tool": r[4],
                "session_model": r[5],
                "operator_open_id": r[6] or "",
                "bot_open_id": r[7] or "",
                "sidecar_mode": bool(r[8]),
            }
            for r in rows
        ]
    finally:
        conn.close()


def set_session_last_checked(session_id, ts):
    """Update the last_checked timestamp for a session.

    Args:
        session_id: The session ID to update.
        ts: Timestamp in milliseconds since epoch (int). Other types are
            converted to int. Invalid values result in NULL.
    """
    ts_value = None
    if ts is not None:
        try:
            if isinstance(ts, int):
                ts_value = ts
            elif isinstance(ts, (str, float)):
                # Use float() first to handle float strings like "123456.78"
                ts_value = int(float(str(ts).strip()))
            else:
                ts_value = None
        except (ValueError, TypeError):
            ts_value = None

    conn = _get_db()
    try:
        conn.execute(
            "UPDATE sessions SET last_checked = ? WHERE session_id = ?",
            (ts_value, session_id),
        )
        conn.commit()
    finally:
        conn.close()


def activate_handoff(session_id, chat_id, session_model, operator_open_id="",
                     bot_open_id="", sidecar_mode=False):
    """Activate handoff for a session (local DB only)."""
    register_session(
        session_id, chat_id,
        session_model=session_model,
        operator_open_id=operator_open_id,
        bot_open_id=bot_open_id,
        sidecar_mode=sidecar_mode,
    )


def deactivate_handoff(session_id):
    """Deactivate handoff for a session (local DB only).

    Returns the chat_id that was associated, or None.
    """
    session = get_session(session_id)
    chat_id = session["chat_id"] if session else None
    unregister_session(session_id)
    return chat_id


def _load_config():
    """Read raw JSON from CONFIG_FILE. Returns dict or None on error."""
    try:
        with open(CONFIG_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def _resolve_im_config(raw):
    """Extract IM-specific credentials from a raw config dict.

    Supports two formats:
      - Nested: {"default_im": "lark", "ims": {"lark": {"app_id": ...}}}
      - Flat (legacy): {"app_id": ..., "app_secret": ..., "email": ...}

    Returns dict with app_id/app_secret/email keys, or None if required
    fields are missing.
    """
    if raw is None:
        return None
    ims = raw.get("ims")
    if isinstance(ims, dict):
        provider = raw.get("default_im", "lark")
        im_cfg = ims.get(provider)
        if not isinstance(im_cfg, dict):
            return None
        if not im_cfg.get("app_id") or not im_cfg.get("app_secret"):
            return None
        return im_cfg
    # Legacy flat format
    if not raw.get("app_id") or not raw.get("app_secret"):
        return None
    return raw


def load_credentials():
    """Load app_id, app_secret, email from config file.

    Returns the config dict, or None if the file doesn't exist or
    required fields (app_id, app_secret) are missing.
    """
    return _resolve_im_config(_load_config())


def load_worker_url():
    """Load the Cloudflare Worker URL from config. Returns None if missing."""
    raw = _load_config()
    if raw is None:
        return None
    url = raw.get("worker_url", "").strip()
    return url or None


def load_api_key():
    """Load the Worker API key from config. Returns None if not set."""
    raw = _load_config()
    if raw is None:
        return None
    return raw.get("worker_api_key", "").strip() or None


def _worker_auth_headers():
    """Return curl args for Worker API auth. Empty list if no key configured."""
    key = load_api_key()
    if key:
        return ["-H", f"Authorization: Bearer {key}"]
    return []


def poll_worker(worker_url, chat_id, since=None, key=None):
    """Long-poll the worker for replies from a handoff group.

    Uses the /poll/ endpoint which blocks up to 25 seconds waiting for
    new replies, returning instantly when data arrives.

    key: optional DO routing key. When provided, polls this key instead of
        ``chat:{chat_id}``. Used by permission bridge to poll a nonce-keyed DO.

    Returns dict with keys: replies (list), takeover (bool), error (str|None).
    """
    import subprocess

    # 25s long-poll: stays under CF Workers' 30s wall-clock limit (5s margin)
    # while minimising reconnections to conserve free-tier quota.
    # curl --max-time 30 gives 5s for the response to arrive after the poll.
    # Python timeout=35 gives 5s for curl to clean up after --max-time fires.
    do_key = key or f"chat:{chat_id}"
    url = f"{worker_url}/poll/{do_key}?timeout=25"
    if since:
        url += f"&since={since}"
    result = subprocess.run(
        ["curl", "-s", "--max-time", "30", *_worker_auth_headers(), url],
        capture_output=True,
        text=True,
        timeout=35,
    )
    if result.returncode != 0:
        return {
            "replies": [],
            "takeover": False,
            "error": f"curl failed (exit {result.returncode})",
        }
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        return {
            "replies": [],
            "takeover": False,
            "error": f"Worker returned non-JSON: {result.stdout[:200]}",
        }
    if data.get("error"):
        return {
            "replies": [],
            "takeover": False,
            "error": f"Worker error: {data['error']}",
        }
    return {
        "replies": data.get("replies", []),
        "takeover": data.get("takeover", False),
        "error": None,
    }


# ---------------------------------------------------------------------------
# WebSocket client (stdlib only — no external dependencies)
# ---------------------------------------------------------------------------


class _WebSocket:
    """Minimal WebSocket client for wss:// connections using only stdlib.

    Supports text frames, ping/pong, and close. Does not support
    extensions, compression, or continuation frames (not needed here —
    all messages are small JSON payloads).
    """

    def __init__(self, url, headers=None):
        parsed = urllib.parse.urlparse(url)
        self.host = parsed.hostname
        self.port = parsed.port or (443 if parsed.scheme == "wss" else 80)
        self.path = parsed.path or "/"
        if parsed.query:
            self.path += "?" + parsed.query
        self.use_tls = parsed.scheme == "wss"
        self.extra_headers = headers or {}
        self._sock = None
        self._buf = b""

    @staticmethod
    def _get_http_proxy(target_host):
        """Detect HTTP(S) proxy from environment (respects https_proxy/no_proxy)."""
        # Check no_proxy — skip proxy for matching hosts
        no_proxy = os.environ.get("no_proxy") or os.environ.get("NO_PROXY") or ""
        if no_proxy:
            for entry in no_proxy.split(","):
                entry = entry.strip().lower()
                if not entry:
                    continue
                host_lower = target_host.lower()
                # "*" matches everything
                if entry == "*":
                    return None, None
                # ".example.com" matches any subdomain; "example.com" matches exact
                if host_lower == entry or host_lower.endswith("." + entry.lstrip(".")):
                    return None, None

        proxy_url = (
            os.environ.get("https_proxy")
            or os.environ.get("HTTPS_PROXY")
            or os.environ.get("http_proxy")
            or os.environ.get("HTTP_PROXY")
        )
        if not proxy_url:
            return None, None
        parsed = urllib.parse.urlparse(proxy_url)
        default_port = 443 if parsed.scheme == "https" else 80
        return parsed.hostname, parsed.port or default_port

    def connect(self, timeout=10):
        """Perform WebSocket upgrade handshake (with HTTP proxy tunneling)."""
        proxy_host, proxy_port = self._get_http_proxy(self.host)

        if proxy_host:
            # HTTP CONNECT tunnel through the proxy
            sock = socket.create_connection(
                (proxy_host, proxy_port),
                timeout=timeout,
            )
            connect_req = (
                f"CONNECT {self.host}:{self.port} HTTP/1.1\r\n"
                f"Host: {self.host}:{self.port}\r\n"
                f"\r\n"
            )
            sock.sendall(connect_req.encode())
            sock.settimeout(timeout)
            resp = b""
            deadline = time.time() + timeout
            MAX_PROXY_RESPONSE = 65536  # 64KB cap to prevent unbounded memory use
            while b"\r\n\r\n" not in resp:
                if time.time() > deadline:
                    sock.close()
                    raise ConnectionError(f"Proxy CONNECT timed out after {timeout}s")
                if len(resp) > MAX_PROXY_RESPONSE:
                    sock.close()
                    raise ConnectionError(
                        f"Proxy response exceeded {MAX_PROXY_RESPONSE} bytes"
                    )
                chunk = sock.recv(4096)
                if not chunk:
                    raise ConnectionError("Proxy closed during CONNECT")
                resp += chunk
            status_line = resp.split(b"\r\n")[0]
            try:
                status_code = int(status_line.split(b" ")[1])
            except (IndexError, ValueError):
                status_code = 0
            if status_code != 200:
                sock.close()
                raise ConnectionError(f"Proxy CONNECT failed: {status_line.decode()}")
        else:
            sock = socket.create_connection(
                (self.host, self.port),
                timeout=timeout,
            )

        if self.use_tls:
            ctx = ssl.create_default_context()
            sock = ctx.wrap_socket(sock, server_hostname=self.host)

        key = base64.b64encode(os.urandom(16)).decode()
        lines = [
            f"GET {self.path} HTTP/1.1",
            f"Host: {self.host}",
            "Upgrade: websocket",
            "Connection: Upgrade",
            f"Sec-WebSocket-Key: {key}",
            "Sec-WebSocket-Version: 13",
            "User-Agent: curl/8.0",
        ]
        for k, v in self.extra_headers.items():
            lines.append(f"{k}: {v}")
        lines.append("")
        lines.append("")
        sock.sendall("\r\n".join(lines).encode())

        # Read response headers with deadline
        sock.settimeout(timeout)
        response = b""
        deadline = time.time() + timeout
        while b"\r\n\r\n" not in response:
            if time.time() > deadline:
                sock.close()
                raise ConnectionError(f"WebSocket handshake timed out after {timeout}s")
            chunk = sock.recv(4096)
            if not chunk:
                raise ConnectionError("Connection closed during handshake")
            response += chunk

        idx = response.index(b"\r\n\r\n") + 4
        self._buf = response[idx:]  # leftover data after headers

        status_line = response[:idx].split(b"\r\n")[0].decode()
        if "101" not in status_line:
            sock.close()
            raise ConnectionError(f"WebSocket upgrade failed: {status_line}")
        # Note: Sec-WebSocket-Accept verification is skipped because CF Workers
        # terminate WebSocket at the edge and proxy a new connection to the DO.
        # The Accept header won't match our key. HTTPS prevents MITM anyway.

        self._sock = sock

    def recv(self, timeout=None):
        """Receive one text message. Returns str, or None on close frame.

        Note: timeout is per-recv syscall, not per-message assembly. For the
        small JSON payloads here (<1KB), each message fits in one recv call,
        so the timeout effectively applies per-message.
        """
        if timeout is not None:
            self._sock.settimeout(timeout)
        while True:
            header = self._recv_exact(2)
            opcode = header[0] & 0x0F
            masked = (header[1] >> 7) & 1
            length = header[1] & 0x7F

            if length == 126:
                length = struct.unpack("!H", self._recv_exact(2))[0]
            elif length == 127:
                length = struct.unpack("!Q", self._recv_exact(8))[0]

            # Guard against oversized frames (expected payloads are <1KB JSON)
            if length > 1_048_576:  # 1 MB
                self.close()
                raise ConnectionError(
                    f"WebSocket frame too large: {length} bytes (max 1MB)"
                )

            mask_key = self._recv_exact(4) if masked else None
            payload = self._recv_exact(length)

            if mask_key:
                payload = bytes(b ^ mask_key[i % 4] for i, b in enumerate(payload))

            if opcode == 0x8:  # Close
                return None
            if opcode == 0x9:  # Ping — auto-respond with pong
                self._send_frame(0xA, payload)
                continue
            if opcode == 0xA:  # Pong — ignore
                continue
            if opcode == 0x1:  # Text
                return payload.decode()
            # Binary or unknown — skip
            continue

    def send(self, text):
        """Send a text message."""
        self._send_frame(0x1, text.encode() if isinstance(text, str) else text)

    def close(self):
        """Send close frame and close the socket."""
        try:
            self._send_frame(0x8, b"")
        except Exception:
            pass
        try:
            self._sock.close()
        except Exception:
            pass
        self._sock = None

    def _send_frame(self, opcode, payload):
        mask = os.urandom(4)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        frame = bytes([0x80 | opcode])
        length = len(payload)
        if length < 126:
            frame += bytes([0x80 | length])
        elif length < 65536:
            frame += bytes([0x80 | 126]) + struct.pack("!H", length)
        else:
            frame += bytes([0x80 | 127]) + struct.pack("!Q", length)
        frame += mask + masked
        self._sock.sendall(frame)

    def _recv_exact(self, n):
        while len(self._buf) < n:
            chunk = self._sock.recv(max(n - len(self._buf), 4096))
            if not chunk:
                self.close()
                raise ConnectionError("Connection closed")
            self._buf += chunk
        result = self._buf[:n]
        self._buf = self._buf[n:]
        return result


def poll_worker_ws(worker_url, chat_id, since=None, max_duration=None, key=None):
    """Connect via WebSocket and wait for replies. Returns on first message.

    Uses a single persistent WebSocket connection instead of repeated HTTP
    long-polls. Dramatically reduces CF Workers request quota usage: 1 request
    per wait cycle instead of 1 every 25 seconds.

    Args:
        max_duration: Optional max seconds to wait before returning empty.
            When set, the WS poll returns ``{"replies": [], "error": None}``
            after this many seconds of no data, allowing callers to check
            their own deadlines. Default ``None`` = wait indefinitely.
        key: Optional DO routing key. When provided, connects to this key
            instead of ``chat:{chat_id}``. Used by permission bridge to poll
            a nonce-keyed DO.

    Returns dict with keys: replies (list), takeover (bool), error (str|None).
    """
    do_key = key or f"chat:{chat_id}"
    ws_url = worker_url.replace("https://", "wss://").replace("http://", "ws://")
    ws_url += f"/ws/{do_key}"
    if since:
        ws_url += f"?since={since}"

    api_key = load_api_key()
    headers = {}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    ws = _WebSocket(ws_url, headers=headers)
    ws.connect(timeout=10)

    ws_start = time.time()
    try:
        while True:
            try:
                # 30s recv timeout — sends keepalive ping on timeout.
                # Many HTTP proxies (e.g. Surge) close idle CONNECT tunnels
                # after ~50s, so we ping well before that. ~120 DO reqs/hr
                # when idle, but each is tiny (pong response).
                msg = ws.recv(timeout=30)
            except socket.timeout:
                # Check max_duration before pinging — allows callers to
                # enforce their own deadlines (e.g. permission bridge timeout).
                if max_duration and (time.time() - ws_start) >= max_duration:
                    return {"replies": [], "takeover": False, "error": None}
                try:
                    ws.send(json.dumps({"ping": True}))
                except Exception:
                    return {"replies": [], "takeover": False, "error": "ping_failed"}
                continue

            if msg is None:  # Close frame
                return {"replies": [], "takeover": False, "error": "ws_closed"}

            try:
                data = json.loads(msg)
            except json.JSONDecodeError:
                continue

            if data.get("pong"):
                continue

            if data.get("takeover"):
                return {"replies": [], "takeover": True, "error": None}

            replies = data.get("replies", [])
            if replies:
                # Ack processed replies over WebSocket (no HTTP request needed)
                last = replies[-1].get("create_time", "")
                if last:
                    try:
                        ws.send(json.dumps({"ack": last}))
                    except Exception:
                        pass
                return {"replies": replies, "takeover": False, "error": None}
    except ConnectionError as e:
        return {"replies": [], "takeover": False, "error": str(e)}
    finally:
        ws.close()


def register_message(worker_url, message_id, chat_id):
    """Register a sent message's chat_id with the worker for reaction routing."""
    import subprocess

    url = f"{worker_url}/register-message"
    payload = json.dumps({"message_id": message_id, "chat_id": chat_id})
    try:
        result = subprocess.run(
            [
                "curl",
                "-s",
                "--max-time",
                "5",
                "-X",
                "POST",
                "-H",
                "Content-Type: application/json",
                *_worker_auth_headers(),
                "-d",
                payload,
                url,
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0:
            print(
                f"[handoff] register_message failed (rc={result.returncode}): {result.stderr}",
                file=sys.stderr,
            )
    except Exception as e:
        print(f"[handoff] register_message error: {e}", file=sys.stderr)


def send_takeover(worker_url, chat_id):
    """Signal the worker to notify any polling session of a takeover.

    Sets a flag in the Durable Object. The next poll from
    wait_for_reply.py will see ``takeover: true`` in the response, causing
    it to exit cleanly so a new session can take over.

    Args:
        worker_url: Base URL of the Cloudflare Worker.
        chat_id: Chat ID of the handoff group being taken over.
    """
    import subprocess

    url = f"{worker_url}/takeover/chat:{chat_id}"
    try:
        result = subprocess.run(
            [
                "curl",
                "-s",
                "--max-time",
                "5",
                "-X",
                "POST",
                *_worker_auth_headers(),
                url,
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0:
            print(
                f"[handoff] send_takeover failed (rc={result.returncode}): {result.stderr}",
                file=sys.stderr,
            )
    except Exception as e:
        # Best-effort — the old session may already be dead
        print(f"[handoff] send_takeover error: {e}", file=sys.stderr)


def ack_worker_replies(worker_url, chat_id, before, key=None):
    """Acknowledge processed replies, removing them from the Durable Object.

    Removes all replies with create_time <= before. This prevents unbounded
    growth of stored replies during long handoff periods.

    Args:
        worker_url: Base URL of the Cloudflare Worker.
        chat_id: Chat ID whose replies to acknowledge.
        before: Timestamp string (ms) — remove replies at or before this time.
        key: Optional DO routing key. When provided, acks this key instead of
            ``chat:{chat_id}``. Used by permission bridge for nonce-keyed DOs.
    """
    import subprocess

    do_key = key or f"chat:{chat_id}"
    url = f"{worker_url}/replies/{do_key}/ack?before={before}"
    try:
        result = subprocess.run(
            [
                "curl",
                "-s",
                "--max-time",
                "5",
                "-X",
                "POST",
                *_worker_auth_headers(),
                url,
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0:
            print(
                f"[handoff] ack_worker_replies failed (rc={result.returncode}): {result.stderr}",
                file=sys.stderr,
            )
    except Exception as e:
        # Non-critical — stale replies just get cleaned up eventually
        print(f"[handoff] ack_worker_replies error: {e}", file=sys.stderr)


def resolve_session_context():
    """Load credentials, get token, and resolve active handoff session.

    Convenience helper that combines the repeated boilerplate of:
    1. load_credentials()
    2. HANDOFF_SESSION_ID from env
    3. get_tenant_token()
    4. get_session() + chat_id extraction

    Returns:
        dict with keys: token, session_id, chat_id, session

    Raises:
        RuntimeError: on any missing or invalid state.
    """
    credentials = load_credentials()
    if not credentials:
        raise RuntimeError("No credentials configured")

    session_id = os.environ.get("HANDOFF_SESSION_ID", "")
    if not session_id:
        raise RuntimeError("HANDOFF_SESSION_ID is not set")

    token = get_tenant_token(credentials["app_id"], credentials["app_secret"])

    session = get_session(session_id)
    if not session:
        raise RuntimeError(f"No active session for {session_id}")

    chat_id = session.get("chat_id")
    if not chat_id:
        raise RuntimeError(f"Session {session_id} has no chat_id")

    return {
        "token": token,
        "session_id": session_id,
        "chat_id": chat_id,
        "session": session,
    }


def get_tenant_token(app_id, app_secret):
    """Get or refresh tenant access token."""
    return _auth._get_tenant_token(app_id, app_secret)


def build_card(title, body="", color="blue", buttons=None, chat_id=None, nonce=None):
    """Build a card dict with optional action buttons.

    buttons: list of (label, action_value, button_type) tuples.
        button_type: "primary", "danger", or "default".
    chat_id: chat ID for routing callbacks.
    nonce: optional unique ID for correlating this card's button clicks
        with the specific poll loop waiting for them.
    """
    elements = []
    if body and body.strip():
        elements.append(
            {
                "tag": "div",
                "text": {"content": body, "tag": "lark_md"},
            }
        )
    _value_base = {
        "chat_id": chat_id or "",
        "title": title,
        "body": body[:500] if body else "",
    }
    if nonce:
        _value_base["nonce"] = nonce
    if buttons:
        actions = []
        for label, action_value, button_type in buttons:
            value = {**_value_base, "action": action_value}
            actions.append(
                {
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": label},
                    "type": button_type,
                    "value": value,
                }
            )
        elements.append({"tag": "action", "actions": actions})
    return {
        "header": {
            "title": {"tag": "plain_text", "content": title},
            "template": color,
        },
        "elements": elements,
    }


def build_markdown_card(content, title="", color=""):
    """Build a Card V2 with markdown content for rich text rendering.

    Uses Card JSON 2.0 schema with a markdown element. Supports full markdown
    including bold, italic, lists, code blocks, inline code, and blockquotes.

    content: markdown text to render.
    title: optional card header title. If empty, no header is shown.
    color: header color template (e.g. "blue", "green", "grey").
    """
    card = {
        "schema": "2.0",
        "config": {"update_multi": True},
        "body": {
            "direction": "vertical",
            "elements": [
                {"tag": "markdown", "content": content},
            ],
        },
    }
    if title:
        card["header"] = {
            "title": {"tag": "plain_text", "content": title},
        }
        if color:
            card["header"]["template"] = color
    return card


def build_form_card(
    title,
    body="",
    color="blue",
    selects=None,
    inputs=None,
    checkers=None,
    submit_label="Submit",
    cancel_label=None,
    chat_id=None,
):
    """Build a Card V2 form card with select menus, inputs, and/or checkers.

    Uses Card JSON 2.0 with a form container. When the user clicks Submit,
    all form values are sent as a single callback with form_value dict.

    selects: list of (name, placeholder, options[, default[, label]]) tuples.
        name: field name in the form_value dict.
        placeholder: placeholder text for the dropdown.
        options: list of (label, value) tuples.
        default: optional default value. If omitted, first option is selected.
        label: optional bold label rendered above the dropdown.
    inputs: list of (name, placeholder) tuples.
        name: field name in the form_value dict.
        placeholder: placeholder text for the input.
    checkers: list of (name, label, checked) tuples.
        name: field name in the form_value dict.
        label: display text for the checkbox.
        checked: default checked state (bool).
    submit_label: text for the submit button.
    cancel_label: text for cancel button (rendered outside form). None to omit.
    chat_id: chat ID for routing callbacks.
    """
    form_elements = []
    if selects:
        for sel in selects:
            name, placeholder, options = sel[0], sel[1], sel[2]
            default = sel[3] if len(sel) > 3 else options[0][1] if options else None
            sel_label = sel[4] if len(sel) > 4 else None
            if sel_label:
                form_elements.append({"tag": "markdown", "content": f"**{sel_label}**"})
            el = {
                "tag": "select_static",
                "name": name,
                "placeholder": {"content": placeholder},
                "options": [
                    {"text": {"content": lbl}, "value": val} for lbl, val in options
                ],
            }
            if default is not None:
                el["initial_option"] = default
            form_elements.append(el)
    if checkers:
        for name, label, checked in checkers:
            form_elements.append(
                {
                    "tag": "checker",
                    "name": name,
                    "checked": checked,
                    "text": {"tag": "plain_text", "content": label},
                }
            )
    if inputs:
        for name, placeholder in inputs:
            form_elements.append(
                {
                    "tag": "input",
                    "name": name,
                    "placeholder": {"content": placeholder},
                }
            )
    form_elements.append(
        {
            "tag": "button",
            "text": {"content": submit_label},
            "type": "primary",
            "action_type": "form_submit",
            "name": "submit",
            "value": {
                "action": "form_submit",
                "chat_id": chat_id or "",
                "title": title,
                "body": body[:500] if body else "",
            },
        }
    )

    body_elements = []
    if body and body.strip():
        body_elements.append({"tag": "markdown", "content": body})
    body_elements.append(
        {
            "tag": "form",
            "name": "form",
            "elements": form_elements,
        }
    )
    if cancel_label:
        body_elements.append(
            {
                "tag": "button",
                "text": {"content": cancel_label},
                "type": "default",
                "value": {
                    "action": "__cancel__",
                    "chat_id": chat_id or "",
                },
            }
        )

    return {
        "schema": "2.0",
        "header": {
            "title": {"tag": "plain_text", "content": title},
            "template": color,
        },
        "body": {"elements": body_elements},
    }


# Lark Card V2 server-side rendering error — intermittent outage
_CARD_CREATE_ERROR = 230099


def _is_v2_card(card):
    return isinstance(card, dict) and card.get("schema") == "2.0"


def _extract_card_text(card):
    """Extract title and body text from a card dict."""
    header = card.get("header", {})
    t = header.get("title", {})
    title = t.get("content", "") if isinstance(t, dict) else str(t)

    parts = []
    if _is_v2_card(card):
        for el in card.get("body", {}).get("elements", []):
            tag = el.get("tag")
            if tag == "markdown":
                parts.append(el.get("content", ""))
            elif tag == "form":
                for fel in el.get("elements", []):
                    if fel.get("tag") == "markdown":
                        parts.append(fel.get("content", ""))
    else:
        for el in card.get("elements", []):
            text = el.get("text", {})
            if isinstance(text, dict):
                parts.append(text.get("content", ""))

    return title, "\n".join(parts)


def _card_to_v1_fallback(card):
    """Convert a card to V1 with a degradation note."""
    title, body = _extract_card_text(card)
    note = "\n\n---\n_Lark Card V2 down — interactive elements disabled_"
    color = card.get("header", {}).get("template", "blue")
    return build_card(title, body=(body + note), color=color)


def _card_to_text_fallback(card):
    """Convert a card to plain text for ultimate fallback."""
    title, body = _extract_card_text(card)
    prefix = f"[{title}]\n" if title else ""
    return prefix + body + "\n(Lark Card V2 down)"


def _im_post(url, token, payload):
    """Send a POST to the Lark IM API. Returns response JSON dict.

    Handles HTTP error responses (4xx/5xx) by reading the JSON body from
    the HTTPError, so callers can inspect ``data["code"]`` for fallback logic.
    """
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
        },
    )
    try:
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read())
        except Exception:
            raise e


def send_message(token, chat_id, card):
    """Send an interactive card to a chat. Returns message_id.

    If card creation fails (error 230099 — Lark Card V2 outage), falls back:
    V2 card → V1 card → plain text.
    """
    url = f"{BASE_URL}/im/v1/messages?receive_id_type=chat_id"
    payload = {
        "receive_id": chat_id,
        "msg_type": "interactive",
        "content": json.dumps(card),
    }
    data = _im_post(url, token, payload)

    if data.get("code") == 0:
        return data["data"]["message_id"]

    if data.get("code") == _CARD_CREATE_ERROR:
        print(
            f"[handoff] Card creation failed (230099), trying V1 fallback",
            file=sys.stderr,
        )
        fallback = _card_to_v1_fallback(card)
        payload["content"] = json.dumps(fallback)
        data = _im_post(url, token, payload)
        if data.get("code") == 0:
            return data["data"]["message_id"]

        # V1 also failed — send as plain text
        print(
            f"[handoff] V1 fallback also failed, sending as text",
            file=sys.stderr,
        )
        text = _card_to_text_fallback(card)
        payload["msg_type"] = "text"
        payload["content"] = json.dumps({"text": text})
        data = _im_post(url, token, payload)
        if data.get("code") == 0:
            return data["data"]["message_id"]

    raise RuntimeError(f"Failed to send message: {data}")


def update_card_message(token, message_id, card):
    """Update (PATCH) an existing card message with new content."""
    url = f"{BASE_URL}/im/v1/messages/{message_id}"
    payload = {"msg_type": "interactive", "content": json.dumps(card)}
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
        },
        method="PATCH",
    )
    try:
        with urllib.request.urlopen(req) as resp:
            data = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        try:
            data = json.loads(e.read())
        except Exception:
            raise e
    if data.get("code") != 0:
        raise RuntimeError(f"Failed to update message: {data}")


def delete_message(token, message_id):
    """Delete a bot-sent message from a chat."""
    url = f"{BASE_URL}/im/v1/messages/{message_id}"
    req = urllib.request.Request(
        url,
        headers={"Authorization": f"Bearer {token}"},
        method="DELETE",
    )
    try:
        with urllib.request.urlopen(req) as resp:
            data = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        try:
            data = json.loads(e.read())
        except Exception:
            raise e
    if data.get("code") != 0:
        raise RuntimeError(f"Failed to delete message: {data}")


def reply_message(token, message_id, card):
    """Reply to a message (creates/continues thread). Returns new message_id.

    Same fallback logic as send_message for card creation failures.
    """
    url = f"{BASE_URL}/im/v1/messages/{message_id}/reply"
    payload = {
        "msg_type": "interactive",
        "content": json.dumps(card),
    }
    data = _im_post(url, token, payload)

    if data.get("code") == 0:
        return data["data"]["message_id"]

    if data.get("code") == _CARD_CREATE_ERROR:
        print(
            f"[handoff] Card creation failed (230099), trying V1 fallback",
            file=sys.stderr,
        )
        fallback = _card_to_v1_fallback(card)
        payload["content"] = json.dumps(fallback)
        data = _im_post(url, token, payload)
        if data.get("code") == 0:
            return data["data"]["message_id"]

        print(
            f"[handoff] V1 fallback also failed, sending as text",
            file=sys.stderr,
        )
        text = _card_to_text_fallback(card)
        payload["msg_type"] = "text"
        payload["content"] = json.dumps({"text": text})
        data = _im_post(url, token, payload)
        if data.get("code") == 0:
            return data["data"]["message_id"]

    raise RuntimeError(f"Failed to reply: {data}")


def list_chat_messages(token, chat_id):
    """List recent messages in a chat. Returns items list."""
    params = urllib.parse.urlencode(
        {
            "container_id_type": "chat",
            "container_id": chat_id,
            "sort_type": "ByCreateTimeDesc",
            "page_size": "50",
        }
    )
    req = urllib.request.Request(
        f"{BASE_URL}/im/v1/messages?{params}",
        headers={"Authorization": f"Bearer {token}"},
    )
    with urllib.request.urlopen(req) as resp:
        data = json.loads(resp.read())

    if data.get("code") != 0:
        raise RuntimeError(f"Failed to list messages: {data}")

    return data.get("data", {}).get("items", [])


def get_thread_replies(token, chat_id, root_message_id):
    """Get human replies to a specific thread.

    Returns:
        List of dicts with keys: text, msg_type, sender_type, create_time,
        message_id, and optionally image_key.
    """
    items = list_chat_messages(token, chat_id)
    replies = []
    for item in items:
        # Only replies to our thread from human users
        if item.get("root_id") != root_message_id:
            continue
        sender = item.get("sender", {})
        if sender.get("sender_type") == "app":
            continue
        create_time = item.get("create_time", "0")
        # Extract content based on message type
        msg_type = item.get("msg_type", "unknown")
        text = ""
        image_key = ""
        try:
            content = json.loads(item.get("body", {}).get("content", "{}"))
        except (json.JSONDecodeError, TypeError):
            content = {}

        if msg_type == "text":
            text = content.get("text", "")
        elif msg_type == "image":
            image_key = content.get("image_key", "")
            text = "[image]"
        else:
            text = f"[{msg_type} message]"

        reply = {
            "text": text,
            "msg_type": msg_type,
            "sender_type": sender.get("sender_type", "unknown"),
            "create_time": create_time,
            "message_id": item.get("message_id", ""),
        }
        if image_key:
            reply["image_key"] = image_key
        replies.append(reply)

    # Return in chronological order (API returns desc)
    replies.reverse()
    return replies


def get_bot_info(token):
    """Get the bot's own info (open_id, app_name). Uses GET /bot/v3/info."""
    req = urllib.request.Request(
        f"{BASE_URL}/bot/v3/info",
        headers={"Authorization": f"Bearer {token}"},
    )
    with urllib.request.urlopen(req) as resp:
        data = json.loads(resp.read())
    if data.get("code") != 0:
        raise RuntimeError(f"Failed to get bot info: {data}")
    bot = data.get("bot", {})
    return {
        "open_id": bot.get("open_id", ""),
        "app_name": bot.get("app_name", ""),
    }


def list_bot_chats(token):
    """List all chats the bot is in. Paginates automatically."""
    all_items = []
    page_token = ""
    while True:
        params = "page_size=100"
        if page_token:
            params += f"&page_token={page_token}"
        req = urllib.request.Request(
            f"{BASE_URL}/im/v1/chats?{params}",
            headers={"Authorization": f"Bearer {token}"},
        )
        with urllib.request.urlopen(req) as resp:
            data = json.loads(resp.read())

        if data.get("code") != 0:
            raise RuntimeError(f"Failed to list chats: {data}")

        all_items.extend(data.get("data", {}).get("items", []))
        if not data.get("data", {}).get("has_more"):
            break
        page_token = data.get("data", {}).get("page_token", "")
        if not page_token:
            break
    return all_items


def create_chat(token, name, description=""):
    """Create a new chat group. Returns chat_id."""
    payload = {
        "name": name,
        "description": description,
        "chat_mode": "group",
    }
    req = urllib.request.Request(
        f"{BASE_URL}/im/v1/chats",
        data=json.dumps(payload).encode(),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
        },
    )
    with urllib.request.urlopen(req) as resp:
        data = json.loads(resp.read())

    if data.get("code") != 0:
        raise RuntimeError(f"Failed to create chat: {data}")

    return data["data"]["chat_id"]


def dissolve_chat(token, chat_id):
    """Dissolve (delete) a chat group. Returns True on success."""
    req = urllib.request.Request(
        f"{BASE_URL}/im/v1/chats/{chat_id}",
        headers={"Authorization": f"Bearer {token}"},
        method="DELETE",
    )
    with urllib.request.urlopen(req) as resp:
        data = json.loads(resp.read())

    if data.get("code") != 0:
        raise RuntimeError(f"Failed to dissolve chat: {data}")

    return True


def update_chat_avatar(token, chat_id, image_path):
    """Upload an image and set it as the chat group avatar."""
    image_key = upload_image(token, image_path, image_type="avatar")
    payload = {"avatar": image_key}
    req = urllib.request.Request(
        f"{BASE_URL}/im/v1/chats/{chat_id}",
        data=json.dumps(payload).encode(),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
        },
        method="PUT",
    )
    with urllib.request.urlopen(req) as resp:
        data = json.loads(resp.read())
    if data.get("code") != 0:
        raise RuntimeError(f"Failed to update chat avatar: {data}")


def add_chat_members(token, chat_id, open_ids):
    """Add members to a chat group by their open_ids."""
    payload = {"id_list": open_ids}
    req = urllib.request.Request(
        f"{BASE_URL}/im/v1/chats/{chat_id}/members?member_id_type=open_id",
        data=json.dumps(payload).encode(),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
        },
    )
    with urllib.request.urlopen(req) as resp:
        data = json.loads(resp.read())

    if data.get("code") != 0:
        raise RuntimeError(f"Failed to add members: {data}")

    return data.get("data", {})


def remove_chat_members(token, chat_id, open_ids):
    """Remove members from a chat group by their open_ids."""
    payload = json.dumps({"id_list": open_ids}).encode()
    req = urllib.request.Request(
        f"{BASE_URL}/im/v1/chats/{chat_id}/members?member_id_type=open_id",
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
        },
        method="DELETE",
    )
    with urllib.request.urlopen(req) as resp:
        data = json.loads(resp.read())

    if data.get("code") != 0:
        raise RuntimeError(f"Failed to remove members: {data}")

    return data.get("data", {})


def get_chat_info(token, chat_id):
    """Get chat group info. Returns dict with name, owner_id, etc."""
    req = urllib.request.Request(
        f"{BASE_URL}/im/v1/chats/{chat_id}",
        headers={"Authorization": f"Bearer {token}"},
    )
    with urllib.request.urlopen(req) as resp:
        data = json.loads(resp.read())

    if data.get("code") != 0:
        raise RuntimeError(f"Failed to get chat info: {data}")

    return data.get("data", {})


def list_chat_tabs(token, chat_id):
    """List chat tabs from left to right order."""
    req = urllib.request.Request(
        f"{BASE_URL}/im/v1/chats/{chat_id}/chat_tabs/list_tabs",
        headers={"Authorization": f"Bearer {token}"},
    )
    with urllib.request.urlopen(req) as resp:
        data = json.loads(resp.read())

    if data.get("code") != 0:
        raise RuntimeError(f"Failed to list chat tabs: {data}")

    return data.get("data", {}).get("chat_tabs", [])


def create_chat_tabs(token, chat_id, chat_tabs):
    """Create chat tabs. Returns full chat_tabs list."""
    payload = {"chat_tabs": chat_tabs}
    req = urllib.request.Request(
        f"{BASE_URL}/im/v1/chats/{chat_id}/chat_tabs",
        data=json.dumps(payload).encode(),
        headers={
            "Content-Type": "application/json; charset=utf-8",
            "Authorization": f"Bearer {token}",
        },
        method="POST",
    )
    with urllib.request.urlopen(req) as resp:
        data = json.loads(resp.read())

    if data.get("code") != 0:
        raise RuntimeError(f"Failed to create chat tabs: {data}")

    return data.get("data", {}).get("chat_tabs", [])


def update_chat_tabs(token, chat_id, chat_tabs):
    """Update chat tabs. Returns full chat_tabs list."""
    payload = {"chat_tabs": chat_tabs}
    req = urllib.request.Request(
        f"{BASE_URL}/im/v1/chats/{chat_id}/chat_tabs/update_tabs",
        data=json.dumps(payload).encode(),
        headers={
            "Content-Type": "application/json; charset=utf-8",
            "Authorization": f"Bearer {token}",
        },
        method="POST",
    )
    with urllib.request.urlopen(req) as resp:
        data = json.loads(resp.read())

    if data.get("code") != 0:
        raise RuntimeError(f"Failed to update chat tabs: {data}")

    return data.get("data", {}).get("chat_tabs", [])


def delete_chat_tabs(token, chat_id, tab_ids):
    """Delete chat tabs by IDs. Returns full chat_tabs list."""
    payload = {"tab_ids": tab_ids}
    req = urllib.request.Request(
        f"{BASE_URL}/im/v1/chats/{chat_id}/chat_tabs/delete_tabs",
        data=json.dumps(payload).encode(),
        headers={
            "Content-Type": "application/json; charset=utf-8",
            "Authorization": f"Bearer {token}",
        },
        method="DELETE",
    )
    with urllib.request.urlopen(req) as resp:
        data = json.loads(resp.read())

    if data.get("code") != 0:
        raise RuntimeError(f"Failed to delete chat tabs: {data}")

    return data.get("data", {}).get("chat_tabs", [])


def sort_chat_tabs(token, chat_id, tab_ids):
    """Sort chat tabs from left to right. Returns full chat_tabs list."""
    payload = {"tab_ids": tab_ids}
    req = urllib.request.Request(
        f"{BASE_URL}/im/v1/chats/{chat_id}/chat_tabs/sort_tabs",
        data=json.dumps(payload).encode(),
        headers={
            "Content-Type": "application/json; charset=utf-8",
            "Authorization": f"Bearer {token}",
        },
        method="POST",
    )
    with urllib.request.urlopen(req) as resp:
        data = json.loads(resp.read())

    if data.get("code") != 0:
        raise RuntimeError(f"Failed to sort chat tabs: {data}")

    return data.get("data", {}).get("chat_tabs", [])


def list_chat_members(token, chat_id):
    """List all members of a chat group. Paginates automatically."""
    all_items = []
    page_token = ""
    while True:
        params = f"member_id_type=open_id&page_size=100"
        if page_token:
            params += f"&page_token={page_token}"
        req = urllib.request.Request(
            f"{BASE_URL}/im/v1/chats/{chat_id}/members?{params}",
            headers={"Authorization": f"Bearer {token}"},
        )
        with urllib.request.urlopen(req) as resp:
            data = json.loads(resp.read())

        if data.get("code") != 0:
            raise RuntimeError(f"Failed to list members: {data}")

        all_items.extend(data.get("data", {}).get("items", []))
        if not data.get("data", {}).get("has_more"):
            break
        page_token = data.get("data", {}).get("page_token", "")
        if not page_token:
            break
    return all_items


def lookup_open_id_by_email(token, email):
    """Look up a user's open_id by their Lark email.

    Uses the contact.v3.user.batch_get_id API. Requires the
    ``contact:user.id:readonly`` scope on the app.

    Args:
        token: Tenant access token.
        email: The user's Lark email address.

    Returns:
        The user's open_id string, or None if not found.
    """
    payload = json.dumps({"emails": [email]}).encode()
    req = urllib.request.Request(
        f"{BASE_URL}/contact/v3/users/batch_get_id?user_id_type=open_id",
        data=payload,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req) as resp:
        data = json.loads(resp.read())
    if data.get("code") != 0:
        raise RuntimeError(f"batch_get_id failed: {data}")
    user_list = data.get("data", {}).get("user_list", [])
    if user_list and user_list[0].get("user_id"):
        return user_list[0]["user_id"]
    return None


def save_credentials(
    app_id=None,
    app_secret=None,
    email=None,
    worker_url=None,
    worker_api_key=None,
):
    """Save credentials to the config file in nested format.

    Auto-migrates legacy flat configs to nested format on write.
    IM-specific fields (app_id, app_secret, email) go under ims.lark;
    infrastructure fields (worker_url, worker_api_key) stay top-level.
    """
    target = default_config_file()
    raw = {}
    if os.path.exists(target):
        try:
            with open(target) as f:
                raw = json.load(f)
        except (json.JSONDecodeError, FileNotFoundError):
            pass

    # Migrate flat → nested if needed
    if "ims" not in raw:
        im_fields = {}
        for key in ("app_id", "app_secret", "email"):
            val = raw.pop(key, None)
            if val:
                im_fields[key] = val
        raw.setdefault("default_im", "lark")
        raw["ims"] = {"lark": im_fields}

    provider = raw.get("default_im", "lark")
    im_cfg = raw["ims"].setdefault(provider, {})

    # Apply IM-specific updates
    if app_id:
        im_cfg["app_id"] = app_id
    if app_secret:
        im_cfg["app_secret"] = app_secret
    if email:
        im_cfg["email"] = email

    # Apply top-level updates
    if worker_url:
        raw["worker_url"] = worker_url
    if worker_api_key:
        raw["worker_api_key"] = worker_api_key

    os.makedirs(os.path.dirname(target), exist_ok=True)
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(raw, f, indent=2)


def upload_image(token, image_path, image_type="message"):
    """Upload an image to Lark. Returns image_key.

    Args:
        token: Tenant access token.
        image_path: Local path to the image file.
        image_type: "message" for chat images, "avatar" for avatars.

    Returns:
        The image_key for the uploaded image.
    """
    import subprocess

    result = subprocess.run(
        [
            "curl",
            "-s",
            "--max-time",
            "30",
            "-X",
            "POST",
            f"{BASE_URL}/im/v1/images",
            "-H",
            f"Authorization: Bearer {token}",
            "-F",
            f"image_type={image_type}",
            "-F",
            f"image=@{image_path}",
        ],
        capture_output=True,
        text=True,
        timeout=35,
    )
    if result.returncode != 0:
        raise RuntimeError(f"Failed to upload image: {result.stderr}")

    data = json.loads(result.stdout)
    if data.get("code") != 0:
        raise RuntimeError(f"Failed to upload image: {data}")

    return data["data"]["image_key"]


def reply_image(token, message_id, image_key):
    """Reply to a message with an image. Returns new message_id."""
    payload = {
        "msg_type": "image",
        "content": json.dumps({"image_key": image_key}),
    }
    req = urllib.request.Request(
        f"{BASE_URL}/im/v1/messages/{message_id}/reply",
        data=json.dumps(payload).encode(),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
        },
    )
    with urllib.request.urlopen(req) as resp:
        data = json.loads(resp.read())

    if data.get("code") != 0:
        raise RuntimeError(f"Failed to reply with image: {data}")

    return data["data"]["message_id"]


def upload_file(token, file_path, file_type="stream"):
    """Upload a file to Lark. Returns file_key.

    Args:
        token: Tenant access token.
        file_path: Local path to the file.
        file_type: One of "opus", "mp4", "pdf", "doc", "xls", "ppt",
                   "stream" (generic). Default "stream".

    Returns:
        The file_key for the uploaded file.
    """
    import subprocess

    file_name = os.path.basename(file_path)
    result = subprocess.run(
        [
            "curl",
            "-s",
            "--max-time",
            "30",
            "-X",
            "POST",
            f"{BASE_URL}/im/v1/files",
            "-H",
            f"Authorization: Bearer {token}",
            "-F",
            f"file_type={file_type}",
            "-F",
            f"file_name={file_name}",
            "-F",
            f"file=@{file_path}",
        ],
        capture_output=True,
        text=True,
        timeout=35,
    )
    if result.returncode != 0:
        raise RuntimeError(f"Failed to upload file: {result.stderr}")

    data = json.loads(result.stdout)
    if data.get("code") != 0:
        raise RuntimeError(f"Failed to upload file: {data}")

    return data["data"]["file_key"]


def reply_file(token, message_id, file_key):
    """Reply to a message with a file attachment. Returns new message_id."""
    payload = {
        "msg_type": "file",
        "content": json.dumps({"file_key": file_key}),
    }
    req = urllib.request.Request(
        f"{BASE_URL}/im/v1/messages/{message_id}/reply",
        data=json.dumps(payload).encode(),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
        },
    )
    with urllib.request.urlopen(req) as resp:
        data = json.loads(resp.read())

    if data.get("code") != 0:
        raise RuntimeError(f"Failed to reply with file: {data}")

    return data["data"]["message_id"]


_MAX_IMAGE_SIZE = 10 * 1024 * 1024  # 10 MB
_MAX_FILE_SIZE = 50 * 1024 * 1024  # 50 MB
_DOWNLOAD_CHUNK = 64 * 1024  # 64 KB


def _download_with_limit(resp, save_path, max_bytes):
    """Stream *resp* to *save_path*, raising if *max_bytes* is exceeded."""
    written = 0
    with open(save_path, "wb") as f:
        while True:
            chunk = resp.read(_DOWNLOAD_CHUNK)
            if not chunk:
                break
            written += len(chunk)
            if written > max_bytes:
                f.close()
                try:
                    os.unlink(save_path)
                except OSError:
                    pass
                raise RuntimeError(
                    f"Download exceeds size limit "
                    f"({written} > {max_bytes} bytes): {save_path}"
                )
            f.write(chunk)
    return save_path


def download_image(token, image_key, message_id):
    """Download an image from Lark by image_key using the message resource API.

    Args:
        token: Tenant access token.
        image_key: The image key (e.g. "img_v3_xxx").
        message_id: The message ID that contains the image.

    Returns:
        The path where the image was saved.
    """
    img_dir = os.path.join(handoff_tmp_dir(), "handoff-images")
    os.makedirs(img_dir, exist_ok=True)
    save_path = os.path.join(img_dir, f"{image_key}.png")

    url = f"{BASE_URL}/im/v1/messages/{message_id}/resources/{image_key}?type=image"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req) as resp:
        _download_with_limit(resp, save_path, _MAX_IMAGE_SIZE)
    return save_path


def download_file(token, file_key, message_id, file_name=None):
    """Download a file from Lark by file_key using the message resource API.

    Args:
        token: Tenant access token.
        file_key: The file key (e.g. "file_v3_xxx").
        message_id: The message ID that contains the file.
        file_name: Original filename. Used to determine save path.

    Returns:
        The path where the file was saved.
    """
    file_dir = os.path.join(handoff_tmp_dir(), "handoff-files")
    os.makedirs(file_dir, exist_ok=True)
    base = _safe_local_filename(file_name or file_key)
    # Prefix with message_id to prevent collisions when different messages
    # have attachments with the same filename.
    msg_prefix = message_id.replace("/", "_")[:20] if message_id else ""
    name = f"{msg_prefix}_{base}" if msg_prefix else base
    save_path = os.path.join(file_dir, name)

    url = f"{BASE_URL}/im/v1/messages/{message_id}/resources/{file_key}?type=file"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req) as resp:
        _download_with_limit(resp, save_path, _MAX_FILE_SIZE)
    return save_path


def _safe_local_filename(name):
    """Return a filesystem-safe basename for local downloads.

    Prevents path traversal by stripping directory components and replacing
    path separators. Falls back to a timestamped name if input is empty.
    """
    raw = str(name or "").strip()
    if not raw:
        return f"file-{int(time.time() * 1000)}"

    normalized = raw.replace("\\", "/")
    base = os.path.basename(normalized)
    if not base or base in (".", ".."):
        base = f"file-{int(time.time() * 1000)}"

    return base.replace("/", "_").replace("\\", "_")


def add_reaction(token, message_id, emoji_type):
    """Add a reaction (emoji) to a message.

    Args:
        token: Tenant access token.
        message_id: The message ID to react to.
        emoji_type: Emoji type string, e.g. "THUMBSUP", "SMILE", "OK",
                    "THANKS", "MUSCLE", "APPLAUSE", "FISTBUMP", "DONE",
                    "LAUGH", "LOL", "LOVE", "FACEPALM", "SOB", "THINKING",
                    "JIAYI", "FINGERHEART", "BLUSH", "SMIRK", "WINK",
                    "PROUD", "WITTY", "SMART", "SCOWL", "CRY", "HAUGHTY",
                    "NOSEPICK", "ERROR".

    Returns:
        The reaction_id.
    """
    payload = {"reaction_type": {"emoji_type": emoji_type}}
    req = urllib.request.Request(
        f"{BASE_URL}/im/v1/messages/{message_id}/reactions",
        data=json.dumps(payload).encode(),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
        },
    )
    with urllib.request.urlopen(req) as resp:
        data = json.loads(resp.read())

    if data.get("code") != 0:
        raise RuntimeError(f"Failed to add reaction: {data}")

    return data.get("data", {}).get("reaction_id", "")


def get_message(token, message_id):
    """Fetch a single message by its ID. Returns the message item dict."""
    req = urllib.request.Request(
        f"{BASE_URL}/im/v1/messages/{message_id}",
        headers={"Authorization": f"Bearer {token}"},
    )
    with urllib.request.urlopen(req) as resp:
        data = json.loads(resp.read())

    if data.get("code") != 0:
        raise RuntimeError(f"Failed to get message: {data}")

    items = data.get("data", {}).get("items", [])
    return items[0] if items else {}


def list_merge_forward_messages(token, message_id):
    """List all child messages inside a merge_forward container.

    Uses GET /im/v1/messages/{message_id} which returns the merge_forward
    message itself plus all child messages. Children have upper_message_id
    set to the merge_forward message_id.

    Args:
        token: Tenant access token.
        message_id: The message_id of the merge_forward message.

    Returns:
        List of child message item dicts in chronological order (excludes
        the merge_forward container itself). Each has keys like msg_type,
        body.content, sender, create_time, upper_message_id, etc.
    """
    req = urllib.request.Request(
        f"{BASE_URL}/im/v1/messages/{message_id}",
        headers={"Authorization": f"Bearer {token}"},
    )
    with urllib.request.urlopen(req) as resp:
        data = json.loads(resp.read())

    if data.get("code") != 0:
        raise RuntimeError(f"Failed to get merge_forward messages: {data}")

    all_items = data.get("data", {}).get("items", [])
    # Filter to only child messages (those with upper_message_id)
    children = [
        item for item in all_items if item.get("upper_message_id") == message_id
    ]
    # Sort by create_time ascending
    children.sort(key=lambda x: x.get("create_time", "0"))
    return children


def extract_message_text(message_item):
    """Extract readable text from a message item returned by the Lark API.

    Handles text, post, image, file, card actions, and other message types.

    Note: wait_for_reply.py does NOT call this function — it returns raw JSON
    and Claude reads the ``text`` field directly. This function is mainly used
    by SKILL.md inline Python code for processing merge_forward child messages.

    Returns:
        A tuple of (text, msg_type).
    """
    msg_type = message_item.get("msg_type", "unknown")
    try:
        content = json.loads(message_item.get("body", {}).get("content", "{}"))
    except (json.JSONDecodeError, TypeError, AttributeError):
        content = {}

    if msg_type == "text":
        return content.get("text", ""), msg_type
    elif msg_type == "post":
        post = content
        if not isinstance(content.get("content"), list):
            locale = next(iter(content), None)
            post = content.get(locale, {}) if locale else {}
        paragraphs = post.get("content", [])
        parts = []
        for para in paragraphs:
            for elem in para:
                if elem.get("text"):
                    parts.append(elem["text"])
                elif elem.get("tag") == "img":
                    parts.append("[image]")
        title = post.get("title", "")
        text = "\n".join(parts)
        if title:
            text = f"{title}\n{text}"
        return text or "[post]", msg_type
    elif msg_type == "image":
        return "[image]", msg_type
    elif msg_type == "file":
        return f"[file: {content.get('file_name', 'unknown')}]", msg_type
    elif msg_type == "interactive":
        # Card message — the Lark API returns Card V2 content in a degraded
        # format (rendered preview image + empty text), so we can only extract
        # whatever text elements survive the conversion.
        title = content.get("title") or ""
        parts = []
        if title:
            parts.append(title)
        for row in content.get("elements", []):
            for elem in row:
                if elem.get("text"):
                    parts.append(elem["text"])
        return "\n".join(parts) or "[card]", msg_type
    elif msg_type in ("button_action", "form_action", "select_action", "input_action"):
        # Card callback actions — these come from the Cloudflare worker when
        # a user clicks a button, submits a form, or selects a dropdown option.
        # The worker stores the action value in the "text" field of the raw
        # JSON, but if someone calls extract_message_text on a Lark API message
        # item for these types, the content is just the action value as text.
        return content.get("text", "") or str(content), msg_type
    elif msg_type == "merge_forward":
        return "[merge_forward]", msg_type
    else:
        return f"[{msg_type} message]", msg_type


# ---------------------------------------------------------------------------
# Message tracking — resolves parent_id on replies and reactions
# ---------------------------------------------------------------------------


def record_sent_message(message_id, text="", title="", chat_id=None):
    """Record a sent message in the local database."""
    if not chat_id:
        raise ValueError("chat_id is required")
    conn = _get_db()
    try:
        conn.execute(
            "INSERT OR REPLACE INTO messages"
            " (message_id, chat_id, direction, source_message_id,"
            "  message_time, text, title, sent_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                message_id,
                chat_id,
                "sent",
                message_id,
                int(time.time() * 1000),
                text,
                title,
                int(time.time()),
            ),
        )
        conn.commit()
    finally:
        conn.close()


def record_received_message(
    chat_id,
    text="",
    title="",
    source_message_id="",
    message_time=None,
):
    """Record a received message in the local database.

    Uses a namespaced primary key to avoid colliding with sent message IDs.
    """
    if not chat_id:
        return
    ts_ms = None
    if message_time is not None:
        try:
            ts_ms = int(str(message_time).strip())
        except ValueError:
            ts_ms = None
    if ts_ms is None:
        ts_ms = int(time.time() * 1000)

    raw_id = str(source_message_id or "").strip()
    if raw_id:
        db_message_id = f"recv:{raw_id}"
    else:
        text_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()[:8]
        db_message_id = f"recv:{chat_id}:{ts_ms}:{text_hash}"

    conn = _get_db()
    try:
        conn.execute(
            "INSERT OR REPLACE INTO messages"
            " (message_id, chat_id, direction, source_message_id,"
            "  message_time, text, title, sent_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                db_message_id,
                chat_id,
                "received",
                raw_id or None,
                ts_ms,
                text,
                title,
                int(time.time()),
            ),
        )
        conn.commit()
    finally:
        conn.close()


def is_bot_sent_message(message_id):
    """Check if a message_id was sent by the bot (exists in messages with direction='sent').

    Used by the sidecar-mode interaction filter to detect replies to bot messages.
    The message_id here is the Lark source_message_id (not the internal DB key).
    """
    if not message_id:
        return False
    conn = _get_db()
    try:
        # Check both by primary key (message_id) and by source_message_id
        row = conn.execute(
            "SELECT 1 FROM messages"
            " WHERE direction = 'sent'"
            "   AND (message_id = ? OR source_message_id = ?)"
            " LIMIT 1",
            (message_id, message_id),
        ).fetchone()
        return row is not None
    finally:
        conn.close()


def default_poll_timeout(session):
    """Return the appropriate poll timeout in seconds based on the session model.

    GPT-based models require a bounded timeout (540s) because their tool-use
    runtime has a hard 600s limit.  All other models (Claude, Gemini, etc.)
    can block indefinitely (0) which reduces background-task churn.
    """
    model = (session or {}).get("session_model", "") or ""
    if "gpt" in model.lower():
        return 540
    return 0


def get_unprocessed_messages(chat_id):
    """Return received messages newer than the last sent message.

    Used on handoff resume to replay messages that were received (recorded in
    DB by handle_result) but never processed by Claude due to an API crash.

    Returns list of dicts: [{text, message_id, create_time, msg_type}, ...],
    matching the same shape as worker poll replies so callers can treat them
    identically.  Returns [] if there are no unprocessed messages.
    """
    conn = _get_db()
    try:
        row = conn.execute(
            "SELECT MAX(message_time) FROM messages"
            " WHERE chat_id = ? AND direction = 'sent'",
            (chat_id,),
        ).fetchone()
        last_sent_ts = row[0] if row and row[0] else 0

        rows = conn.execute(
            "SELECT text, source_message_id, message_time FROM messages"
            " WHERE chat_id = ? AND direction = 'received'"
            "   AND message_time > ?"
            " ORDER BY message_time ASC",
            (chat_id, last_sent_ts),
        ).fetchall()
        return [
            {
                "text": r[0] or "",
                "message_id": r[1] or "",
                "create_time": str(r[2]) if r[2] else "",
                "msg_type": "text",
                "sender_type": "user",
            }
            for r in rows
        ]
    finally:
        conn.close()


def lookup_parent_message(parent_id):
    """Look up a sent message by its message_id. Returns dict or None."""
    conn = _get_db()
    try:
        row = conn.execute(
            "SELECT message_id, text, title, sent_at FROM messages"
            " WHERE message_id = ? AND direction = 'sent'",
            (parent_id,),
        ).fetchone()
        if row:
            return {
                "message_id": row[0],
                "text": row[1],
                "title": row[2],
                "sent_at": row[3],
            }
        return None
    finally:
        conn.close()


def reply_sticker(token, message_id, file_key):
    """Reply to a message with a sticker. Returns new message_id.

    Args:
        token: Tenant access token.
        message_id: The message ID to reply to.
        file_key: The sticker's file_key.
    """
    payload = {
        "msg_type": "sticker",
        "content": json.dumps({"file_key": file_key}),
    }
    req = urllib.request.Request(
        f"{BASE_URL}/im/v1/messages/{message_id}/reply",
        data=json.dumps(payload).encode(),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
        },
    )
    with urllib.request.urlopen(req) as resp:
        data = json.loads(resp.read())

    if data.get("code") != 0:
        raise RuntimeError(f"Failed to reply with sticker: {data}")

    return data["data"]["message_id"]
