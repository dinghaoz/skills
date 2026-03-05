#!/usr/bin/env python3
"""SessionStart hook: persist session ID, project dir, and resume active handoff.

When a Claude session starts or resumes, this hook:
1. Writes HANDOFF_SESSION_ID, HANDOFF_SESSION_TOOL, and HANDOFF_PROJECT_DIR
   to CLAUDE_ENV_FILE so scripts know their ID, tool, and project root.
2. If handoff is active AND owned by this session, silences terminal
   notifications and outputs context for Claude to resume the handoff loop.
"""

import json
import os
import shlex
import subprocess
import sys
import time

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

import lark_im


def warn(msg):
    print(f"[handoff] {msg}", file=sys.stderr)


def main():
    try:
        hook_input = json.loads(sys.stdin.read())
    except Exception as e:
        warn(f"invalid SessionStart hook input JSON: {e}")
        hook_input = {}

    session_id = hook_input.get("session_id", "")

    # Persist session_id and project dir as env vars for subsequent Bash commands
    env_file = os.environ.get("CLAUDE_ENV_FILE")
    project_dir = os.environ.get("CLAUDE_PROJECT_DIR")
    if env_file and session_id:
        try:
            managed_prefixes = (
                "export HANDOFF_SESSION_ID=",
                "export HANDOFF_SESSION_TOOL=",
                "export HANDOFF_PROJECT_DIR=",
            )
            lines = []
            if os.path.exists(env_file):
                with open(env_file) as f:
                    lines = [
                        l
                        for l in f.readlines()
                        if not l.startswith(managed_prefixes)
                    ]
            lines.append(f"export HANDOFF_SESSION_ID={shlex.quote(session_id)}\n")
            lines.append(f"export HANDOFF_SESSION_TOOL={shlex.quote('Claude Code')}\n")
            if project_dir:
                lines.append(f"export HANDOFF_PROJECT_DIR={shlex.quote(project_dir)}\n")
            with open(env_file, "w") as f:
                f.writelines(lines)
        except Exception as e:
            warn(f"failed to persist env vars to env file: {e}")

    # Write per-session cache file as fallback for session_id resolution.
    # CLAUDE_ENV_FILE is empty for globally-installed SessionStart hooks (known Claude Code
    # bug: https://github.com/anthropics/claude-code/issues/15840), so we can't rely on it.
    # We store ppid (= Claude Code process PID) so enter_handoff.py can identify the correct
    # session when multiple sessions are running simultaneously.
    if session_id:
        try:
            sessions_dir = os.path.expanduser("~/.handoff/sessions")
            os.makedirs(sessions_dir, exist_ok=True)
            cache_file = os.path.join(sessions_dir, f"{session_id}.json")
            with open(cache_file, "w") as f:
                json.dump({
                    "session_id": session_id,
                    "project_dir": project_dir or "",
                    "session_tool": "Claude Code",
                    "written_at": time.time(),
                    "ppid": os.getppid(),
                }, f)
            # Clean up session files older than 24h to avoid accumulation
            cutoff = time.time() - 86400
            for fname in os.listdir(sessions_dir):
                fpath = os.path.join(sessions_dir, fname)
                try:
                    if os.path.getmtime(fpath) < cutoff:
                        os.unlink(fpath)
                except Exception:
                    pass
        except Exception as e:
            warn(f"failed to write session cache: {e}")

    # Check if this session has an active handoff
    session = lark_im.get_session(session_id) if session_id else None
    if not session:
        return

    # Handoff is active for this session — silence terminal
    try:
        subprocess.run(
            [sys.executable, os.path.join(SCRIPT_DIR, "iterm2_silence.py"), "on"],
            timeout=5,
        )
    except Exception as e:
        warn(f"failed to silence terminal notifications: {e}")

    # Output full loop instructions so Claude can resume handoff even after
    # context compaction (where SKILL.md content may have been summarized away).
    chat_id = session.get("chat_id", "")
    print(
        f"[Handoff active] This session has an active Lark handoff "
        f"(chat_id: {chat_id}). Resume the handoff main loop NOW.\n"
        f"\n"
        f"CRITICAL RULES:\n"
        f"- ALL Bash commands calling Python scripts with network access MUST use "
        f"dangerouslyDisableSandbox: true (sandbox blocks Lark API)\n"
        f"- NEVER use AskUserQuestion or EnterPlanMode — the user is on Lark, not CLI\n"
        f"- Send ALL responses to Lark via send_to_group.py\n"
        f"\n"
        f"LOOP STEPS:\n"
        f"1. Wait for Lark message:\n"
        f"   python3 .claude/skills/handoff/scripts/wait_for_reply.py --timeout 0\n"
        f"   (dangerouslyDisableSandbox: true, Bash timeout: 600000)\n"
        f"2. If reply is 'handback' → exit handoff (send goodbye, deactivate, restore notifications)\n"
        f"3. Process the user's request (read files, edit code, run commands, etc.)\n"
        f"4. Send response to Lark:\n"
        f"   python3 .claude/skills/handoff/scripts/send_to_group.py '<response>'\n"
        f"   (dangerouslyDisableSandbox: true)\n"
        f"5. Go to step 1\n"
        f"\n"
        f"For full protocol details, read .claude/skills/handoff/SKILL.md"
    )


if __name__ == "__main__":
    main()
