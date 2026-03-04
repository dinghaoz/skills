#!/usr/bin/env python3
"""PreToolUse hook for Bash: auto-approve only when handoff is active.

When handoff mode is active for this session, outputs {"decision": "approve"}
so that Bash commands bypass the interactive permission prompt (the user is on
mobile and approves via Lark instead through the PermissionRequest hook).

When handoff mode is NOT active, exits without output so normal permission
checking applies.
"""

import json
import os
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

import lark_im


def main():
    try:
        hook_input = json.loads(sys.stdin.read())
    except Exception:
        sys.exit(0)

    session_id = hook_input.get("session_id", "")
    if not session_id:
        sys.exit(0)

    session = lark_im.get_session(session_id)
    if not session:
        sys.exit(0)

    # Handoff is active — approve so permission goes through PermissionRequest
    # hook (permission_bridge.py) rather than the interactive CLI prompt.
    print('{"decision": "approve"}')


if __name__ == "__main__":
    main()
