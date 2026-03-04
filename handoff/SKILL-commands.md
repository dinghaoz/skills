# Handoff Sub-commands

Reference for all `/handoff` sub-command implementations. Read by `SKILL.md` on demand.

Prefer deterministic helpers in `python3 .claude/skills/handoff/scripts/handoff_ops.py ...` over inline snippets.

## List Groups (`/handoff chats`)

```bash
python3 .claude/skills/handoff/scripts/handoff_ops.py list-groups --scope user
```

Print as a formatted table. Do NOT enter Handoff mode.

## List All Groups (`/handoff chats_admin`)

```bash
python3 .claude/skills/handoff/scripts/handoff_ops.py list-groups --scope all
```

Print as a formatted table. Do NOT enter Handoff mode.

## Status (`/handoff status`)

```bash
python3 .claude/skills/handoff/scripts/handoff_ops.py status
```

Print workspace status in a fixed pretty format (workspace, DB, groups, session details). Use `--format json` for machine-readable output. Do NOT enter Handoff mode.

## Delete Group (`/handoff delete_admin [group name]`)

**Guard:** Cannot run during handoff mode. Refuse and ask user to send `handback` first.

1. Discover candidate groups with `list-groups --scope user`.
2. Filter by provided group name substring (case-insensitive) if given.
3. Ask for confirmation / selection.
4. For each selected chat:

```bash
python3 .claude/skills/handoff/scripts/handoff_ops.py dissolve-chat --chat-id '<CHAT_ID>'
python3 .claude/skills/handoff/scripts/handoff_ops.py cleanup-sessions --chat-id '<CHAT_ID>'
```

Print summary and stop.

## Purge Empty Groups (`/handoff purge_admin`)

**Guard:** Cannot run during handoff mode (ask user to send `handback` first).

1. Discover empty groups:

```bash
python3 .claude/skills/handoff/scripts/handoff_ops.py find-empty-groups
```

2. If none: report and stop.
3. Ask for confirmation.
4. For each selected chat:

```bash
python3 .claude/skills/handoff/scripts/handoff_ops.py dissolve-chat --chat-id '<CHAT_ID>'
python3 .claude/skills/handoff/scripts/handoff_ops.py cleanup-sessions --chat-id '<CHAT_ID>'
```

Print summary and stop.

## Deinit (`/handoff deinit`)

Reverse of `/handoff init`: remove installed hooks or plugin files, then optionally delete config.

1. Confirm with user.

**Claude Code path:**
2. Remove all handoff hooks (`PreToolUse`, `Notification`, `PermissionRequest`, `PostToolUse`, `PostToolUseFailure`, `PreCompact`, `SessionStart`, `SessionEnd`) from `.claude/settings.json` and `.claude/settings.local.json` via `Edit` tool. For each event type, remove only entries whose `command` references `.claude/skills/handoff/scripts/`. Leave non-handoff entries untouched.

**OpenCode path:**
2. Restore plugin files back to the skill's assets directory, then remove from `.opencode/`:
   ```bash
   SKILL=".claude/skills/handoff"
   mkdir -p "$SKILL/assets/opencode/plugins" "$SKILL/assets/opencode/scripts"
   cp .opencode/plugins/handoff.ts "$SKILL/assets/opencode/plugins/"
   cp .opencode/scripts/permission_bridge.py "$SKILL/assets/opencode/scripts/"
   cp .opencode/scripts/handoff_tool_forwarding.js "$SKILL/assets/opencode/scripts/"
   rm -f .opencode/plugins/handoff.ts \
         .opencode/scripts/permission_bridge.py \
         .opencode/scripts/handoff_tool_forwarding.js
   ```

**Both paths:**
3. Ask: "Also delete `~/.handoff/config.json`? (y/N)" — default **No**. If yes:
   ```bash
   python3 .claude/skills/handoff/scripts/handoff_ops.py deinit-config
   ```
4. Print a summary of what was removed and stop.

## Clear (`/handoff clear`)

Deletes current project's chat group(s) and handoff DB.

1. Confirm with user.
2. Run deterministic clear:

```bash
python3 .claude/skills/handoff/scripts/handoff_ops.py clear-project
```

3. Print summary and stop.

## Diagnostic (`/handoff diag`)

Tests the permission bridge end-to-end: sends a card with Approve/Deny buttons, polls for the user's click, and reports whether the round-trip works.

```bash
python3 .claude/skills/handoff/scripts/handoff_ops.py diag --mode ws --timeout 60
```

Options:
- `--mode ws` (default): Poll via WebSocket only
- `--mode http`: Poll via HTTP long-poll only
- `--mode both`: Try WebSocket first, fall back to HTTP
- `--chat-id <ID>`: Target a specific chat (auto-detected if omitted)
- `--timeout <N>`: Max seconds to wait for a button click (default: 60, only used for HTTP)

Outputs JSON with a `steps` array showing each stage (credentials, worker, ack, send_card, poll). The `ok` field indicates overall success. If the poll step fails, the card action callback may not be configured in the Lark app, or the poll method may have issues (compare `--mode ws` vs `--mode http`).

This MUST run with `dangerouslyDisableSandbox: true` (Claude Code only — opencode has no sandbox). Print the JSON output to the user. Do NOT enter Handoff mode.

## Test Commands

- Log health check (plugin + permission bridge):

```bash
python3 .claude/skills/handoff/scripts/handoff_ops.py log-check --lines 4000
```

Recent-window check (best effort):

```bash
python3 .claude/skills/handoff/scripts/handoff_ops.py log-check --lines 4000 --since-minutes 30
```

- Single CI-friendly command (syntax + unit + simulation):

```bash
python3 .claude/skills/handoff/scripts/run_tests.py
```

- Unit + simulation tests only:

```bash
python3 -m unittest discover -s .claude/skills/handoff/scripts/tests -p 'test_*.py'
```

- Syntax check for scripts + tests:

```bash
python3 -m py_compile .claude/skills/handoff/scripts/*.py .claude/skills/handoff/scripts/tests/test_*.py
```
