#!/usr/bin/env python3
"""Single-shot entry point for entering handoff mode.

Runs Steps A→B→C(auto)→D and returns one of:
  {"status": "ready",          "chat_id": "...", "session_id": "...", "project_dir": "..."}
  {"status": "already_active", "chat_id": "...", "session_id": "..."}
  {"status": "choose",         "groups": [...],  "reason": "all_occupied" | "multiple_inactive"}

"ready" means activate completed — caller should run start_and_wait.py.
"choose" means Claude must ask the user which group to use, then call
  handoff_ops.py activate --chat-id <...> --session-model <...>
  followed by start_and_wait.py.
"already_active" means this session already has a live handoff.

Env vars resolved automatically if not already set:
  HANDOFF_PROJECT_DIR  — falls back to CLAUDE_PROJECT_DIR, then cwd
  HANDOFF_SESSION_ID   — falls back to a freshly generated UUID
"""

import argparse
import json
import os
import sys
import uuid

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

import lark_im
from send_to_group import (
    create_handoff_group,
    find_groups_for_workspace,
    get_worktree_name,
)


def _jprint(obj):
    print(json.dumps(obj, ensure_ascii=True))


def _resolve_env():
    """Ensure HANDOFF_PROJECT_DIR and HANDOFF_SESSION_ID are set in the process env."""
    if not os.environ.get("HANDOFF_PROJECT_DIR"):
        fallback = os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()
        os.environ["HANDOFF_PROJECT_DIR"] = fallback

    if not os.environ.get("HANDOFF_SESSION_ID"):
        os.environ["HANDOFF_SESSION_ID"] = str(uuid.uuid4())


def _pick_inactive(groups):
    """Return the most recently active inactive group, or first if no timestamps."""
    inactive = [g for g in groups if not g.get("active")]
    if not inactive:
        return None
    # Prefer most recent last_checked, fall back to activated_at, then first
    def sort_key(g):
        lc = g.get("last_checked") or 0
        aa = g.get("activated_at") or 0
        return (lc, aa)
    return max(inactive, key=sort_key)


def main():
    p = argparse.ArgumentParser(description="Enter handoff mode (Steps A-D)")
    p.add_argument("--session-model", required=True, help="Model name for status card")
    p.add_argument(
        "--mode",
        choices=["default", "no-ask", "new"],
        default="default",
        help="Group selection mode",
    )
    args = p.parse_args()

    # Resolve env vars so all subsequent lark_im calls work
    _resolve_env()
    project_dir = os.environ["HANDOFF_PROJECT_DIR"]
    session_id = os.environ["HANDOFF_SESSION_ID"]

    # ── Step A: session-check ──────────────────────────────────────────────
    session = lark_im.get_session(session_id)
    if session:
        _jprint({
            "status": "already_active",
            "chat_id": session.get("chat_id", ""),
            "session_id": session_id,
            "project_dir": project_dir,
        })
        return 0

    # ── Step B: discover ──────────────────────────────────────────────────
    creds = lark_im.load_credentials()
    if not creds:
        _jprint({"error": "no_credentials"})
        return 1
    token = lark_im.get_tenant_token(creds["app_id"], creds["app_secret"])
    email = creds.get("email", "")
    open_id = lark_im.lookup_open_id_by_email(token, email) if email else ""
    workspace_id = lark_im.get_workspace_id()
    groups = find_groups_for_workspace(token, workspace_id, open_id or None)
    lark_im.prune_stale_sessions()
    sessions = lark_im.get_active_sessions()
    session_by_chat = {s["chat_id"]: s for s in sessions}

    enriched = []
    for g in groups:
        chat_id = g.get("chat_id", "")
        sess = session_by_chat.get(chat_id)
        enriched.append({
            "chat_id": chat_id,
            "name": g.get("name", ""),
            "active": bool(sess),
            "last_checked": sess.get("last_checked") if sess else None,
            "activated_at": sess.get("activated_at") if sess else None,
            "session_tool": sess.get("session_tool") if sess else "",
            "session_model": sess.get("session_model") if sess else "",
        })
    enriched.sort(key=lambda x: (x.get("name", ""), x.get("chat_id", "")))

    # ── Step C: decision tree ─────────────────────────────────────────────
    chat_id_to_activate = None

    if args.mode == "new":
        # Always create a new group
        existing_names = [g["name"] for g in enriched]
        machine = lark_im._get_machine_name()
        worktree = get_worktree_name()
        chat_id_to_activate = create_handoff_group(
            token, open_id, worktree, machine, existing_names, workspace_id=workspace_id
        )

    elif args.mode == "no-ask":
        best = _pick_inactive(enriched)
        if best:
            chat_id_to_activate = best["chat_id"]
        else:
            existing_names = [g["name"] for g in enriched]
            machine = lark_im._get_machine_name()
            worktree = get_worktree_name()
            chat_id_to_activate = create_handoff_group(
                token, open_id, worktree, machine, existing_names, workspace_id=workspace_id
            )

    else:  # default
        n = len(enriched)
        if n == 0:
            # Auto-create
            machine = lark_im._get_machine_name()
            worktree = get_worktree_name()
            chat_id_to_activate = create_handoff_group(
                token, open_id, worktree, machine, [], workspace_id=workspace_id
            )
        else:
            inactive = [g for g in enriched if not g.get("active")]
            occupied = [g for g in enriched if g.get("active")]

            if len(inactive) == 1 and len(occupied) == 0:
                # Exactly one group, inactive — auto-select, no prompt
                chat_id_to_activate = inactive[0]["chat_id"]
            elif len(inactive) >= 1:
                # Multiple inactive: auto-pick most recent (same as no-ask)
                best = _pick_inactive(enriched)
                chat_id_to_activate = best["chat_id"]
            else:
                # All occupied — Claude must ask
                _jprint({
                    "status": "choose",
                    "groups": enriched,
                    "reason": "all_occupied",
                    "session_id": session_id,
                    "project_dir": project_dir,
                })
                return 0

    # ── Step D: activate ─────────────────────────────────────────────────
    model = str(args.session_model).strip()
    if "/" in model:
        model = model.split("/", 1)[1]

    operator_open_id = ""
    bot_open_id = ""
    try:
        operator_open_id = open_id
        bot_info = lark_im.get_bot_info(token)
        bot_open_id = bot_info.get("open_id", "")
    except Exception:
        pass

    lark_im.activate_handoff(
        session_id,
        chat_id_to_activate,
        session_model=model,
        operator_open_id=operator_open_id,
        bot_open_id=bot_open_id,
        sidecar_mode=False,
    )

    _jprint({
        "status": "ready",
        "chat_id": chat_id_to_activate,
        "session_id": session_id,
        "project_dir": project_dir,
    })
    return 0


if __name__ == "__main__":
    sys.exit(main())
