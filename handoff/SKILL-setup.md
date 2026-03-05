# Guided Setup

**Principles:**
- **Automate everything you can.** Run commands yourself, parse output, update files — don't ask the user to do things you can do.
- **Only ask the user when human interaction is truly required:** browser-based login, Lark console UI actions, or choosing between options.
- **Use the user's language** for all guidance text.
- **Tell the user what you're doing** as you go (brief status messages before each automated action).

This file is only used by `/handoff init`. The `/handoff` command (no args) does NOT run setup — it runs preflight and tells the user to run `/handoff init` if anything is missing.

When invoked via `/handoff init`: run ALL steps. For each step where a value already exists, use **AskUserQuestion** with an additional first option: **"Keep existing: `<current_value>`"** (showing the current value, redacted for secrets — e.g. show only last 4 chars of app_secret). If the user chooses to keep existing, skip that step.

**Deferred save:** During Steps 1–4, do NOT write to the config file or hooks files. Collect all values in memory. Infrastructure side effects (worker deploy, wrangler secrets) happen inline since they can't be deferred. After Step 4, show a summary and ask for confirmation before applying (see "After setup").

## Before you begin: load existing values

**Before** starting Step 1, run this command to load any existing config values. This handles both the nested `ims.lark` format and the legacy flat format:

```bash
python3 -c "
import sys, json, os
for p in ['.claude/skills/handoff/scripts', os.path.expanduser('~/.claude/skills/handoff/scripts')]:
    if os.path.exists(p):
        sys.path.insert(0, p)
        break
import lark_im
cfg = lark_im._load_config() or {}
im = lark_im._resolve_im_config(cfg) or {}
print(json.dumps({
    'worker_url': cfg.get('worker_url', ''),
    'worker_api_key': cfg.get('worker_api_key', ''),
    'app_id': im.get('app_id', ''),
    'app_secret': im.get('app_secret', ''),
    'email': im.get('email', ''),
}))
"
```

Store the output JSON as `existing`. Use these values when showing "Keep existing" options in each step. A non-empty string means the value exists.

## Step 1: worker_url + worker_api_key

The worker has no dependency on the Lark app, so deploy it first. The URL is needed when configuring the Lark app in Step 2. The `worker_url` and `worker_api_key` are always set together as a pair.

Use **AskUserQuestion** with two options: "Enter existing worker URL and API key" (description: "Provide the URL and API key from an already-deployed worker") and "Create a new Cloudflare Worker" (description: "Deploy a new worker from the project template").

**`/handoff init` with existing values** — When both `worker_url` and `worker_api_key` already exist, add "Keep existing" as the first option (showing the current URL and redacted key).

**Use existing path** — The user selected "Enter existing". Wait for them to provide the `worker_url` and `worker_api_key` in the conversation (do not prompt with another AskUserQuestion). Remember the values — do NOT save yet.

**Create path** — automate as much as possible:
1. Check if `npx wrangler` is available. If not, run `npm install -g wrangler`.
2. Check if the user is logged in: `npx wrangler whoami`. If not logged in, tell the user "You need to login to Cloudflare" and run `npx wrangler login` (this opens a browser — the only manual step).
3. Create a KV namespace automatically:
   ```bash
   cd .claude/skills/handoff/worker && npx wrangler kv namespace create LARK_REPLIES
   ```
   Parse the output to extract the KV namespace ID.
4. Update `.claude/skills/handoff/worker/wrangler.toml` with the new KV ID using the Edit tool.
5. Deploy:
   ```bash
   cd .claude/skills/handoff/worker && npx wrangler deploy
   ```
   Parse the deployed URL from the output.
6. Generate a `worker_api_key` and store it as a Cloudflare Worker secret:
   ```bash
   python3 -c "import secrets; print(secrets.token_urlsafe(32))"
   ```
   Capture the output as `<API_KEY>`, then:
   ```bash
   echo '<API_KEY>' | npx wrangler secret put API_KEY --config .claude/skills/handoff/worker/wrangler.toml
   ```
7. Remember both `worker_url` and `worker_api_key` for the summary — do NOT save yet.

## Step 2: app_id / app_secret

The worker URL from Step 1 is needed here to configure event subscriptions and card callbacks in one pass. The Lark app must be created and configured in the Lark console (web UI) — this cannot be automated.

Use **AskUserQuestion** with options: "Create a new Lark/Feishu app" vs "Use an existing Lark app".

**Create path** — print the full guide (in the user's language), including the worker URL from Step 1 so the user can copy-paste it:
1. Go to [open.larksuite.com/app](https://open.larksuite.com/app) (or [open.feishu.cn/app](https://open.feishu.cn/app) for Feishu)
2. Click **Create Custom App**, give it a name (e.g. "Claude Handoff")
3. Go to **Credentials** page → copy the **App ID** and **App Secret**
4. Go to **Permissions & Scopes** → add these scopes:
   - `contact:user.id:readonly` — Obtain user ID via email or mobile number
   - `im:chat` — Obtain and update group information
   - `im:message` — Read and send messages in private and group chats
   - `im:message.group_msg` — Read all messages in associated group chat (sensitive scope)
   - `im:message.group_msg:readonly` — Obtain all messages in the associated group chats
   - `im:message:readonly` — Read messages in private and group chats
   - `im:message:send_as_bot` — Send messages as an app
   - `im:resource` — Read and upload images or other files
5. Go to **Bot** section → enable bot capability
6. Go to **Event Subscriptions**:
   - Set Request URL to: `{worker_url}/webhook`
   - Subscribe to events:
     - `im.message.receive_v1` (Message received)
     - `im.message.reaction.created_v1` (Message reaction created)
7. Go to **Bot** → **Interactive Features** (or **Card Request URL**):
   - Set Card Request URL to: `{worker_url}/card-action`
8. Click **Create a Version** → **Publish** (approve if prompted)

Then use **AskUserQuestion** to collect `app_id` and `app_secret`. Remember the values — do NOT save yet.

**Use existing path** — Use **AskUserQuestion** to collect `app_id` and `app_secret`. Remind the user to verify event subscription URL (`{worker_url}/webhook`) and card callback URL (`{worker_url}/card-action`) are configured. Remember the values — do NOT save yet.

## Step 3: email

Use **AskUserQuestion** to collect the user's **Lark login email**. Emphasize: this is the **personal email** used to sign in to Lark (e.g. `name@gmail.com`, `name@outlook.com`), **NOT the corporate/enterprise email** (e.g. `name@company.com`). Corporate emails will not work with the lookup API.

Remember the email — do NOT save yet. The `open_id` will be resolved on demand when creating a chat group.

Warn: "All Claude handoff messages from this machine will be sent to this email's Lark account."

## Step 4: Tool-specific integration

**OpenCode users:** Install the plugin files from the skill's assets directory, then delete the assets (they are a distribution snapshot, not a permanent copy):

```bash
SKILL=".claude/skills/handoff"
mkdir -p .opencode/plugins .opencode/scripts
cp "$SKILL/assets/opencode/plugins/handoff.ts" .opencode/plugins/
cp "$SKILL/assets/opencode/scripts/permission_bridge.py" .opencode/scripts/
cp "$SKILL/assets/opencode/scripts/handoff_tool_forwarding.js" .opencode/scripts/
rm -rf "$SKILL/assets/opencode"
```

Verify the files were copied and the assets directory is gone. In the summary table, show **"OpenCode plugin"** in place of "hooks". Skip the rest of this step.

Tell the user: **"Plugin installed. Please exit and reopen OpenCode — plugins are loaded at startup."**

> **On upgrade:** copy the new skill version into `.claude/skills/handoff/` (which restores the assets), then run `/handoff init` again — it detects the assets and overwrites the installed files.

---

**Claude Code users:** Install hooks into a Claude settings file.

**Detect install scope first:**

- **Project install**: `.claude/skills/handoff/hooks.json` exists in the current directory → hooks.json is here, settings targets are `.claude/settings.json` / `.claude/settings.local.json`.
- **Global install**: only `~/.claude/skills/handoff/hooks.json` exists → hooks.json is there, settings targets are `~/.claude/settings.json` / `~/.claude/settings.local.json`.

Use the scope-appropriate paths throughout the rest of this step.

**Resolve hook command strings by scope:**

- **Project install**: use hook command strings from `hooks.json` as-is. Each command uses `git rev-parse --absolute-git-dir` to locate the main project's `.git` directory and resolve scripts relative to it. This works correctly from both the main worktree and any git worktree (e.g. created with `/mkwt`). If the script file doesn't exist, the hook exits 0 silently — safe to run from any project.
- **Global install**: replace each entire `command` string with a literal path. Extract the script filename from the `command` in `hooks.json`, then construct: `python3 "<expanded-HOME>/.claude/skills/handoff/scripts/<script-name>.py"`. Expand `$HOME` to the actual path (e.g. `/Users/alice`), so the commands contain literal paths that work in any project.

**Determine target file:**
1. Read both candidate settings files for the detected scope (missing files count as empty).
2. Check whether each file has a `hooks` key with any entries (handoff or otherwise).
3. Choose the target file:
   - Only one file has a `hooks` key → use that file (no need to ask).
   - Both files have a `hooks` key → prefer `settings.json`; if that would be surprising (e.g. the only hooks are in `settings.local.json`), use **AskUserQuestion** to let the user pick.
   - Neither file has a `hooks` key:
     - **Global install**: auto-select `~/.claude/settings.local.json` — no need to ask (handoff config is machine-specific, so keeping hooks local makes sense).
     - **Project install**: use **AskUserQuestion** with two options:
       - `settings.local.json` — machine-local, not committed to git (Recommended)
       - `settings.json` — shared, committed to git with the project

Remember the chosen file — do NOT apply yet.

**Plan the merge:**

For each hook event type defined in `hooks.json` (currently: `PreToolUse`, `Notification`, `PermissionRequest`, `PostToolUse`, `PostToolUseFailure`, `PreCompact`, `SessionStart`, `SessionEnd`):

1. Look at the target file's existing array for that event type (may be absent or may contain entries for other tools).
2. For each handoff hook entry in `hooks.json` (with paths adjusted for install scope), check whether an entry with the **same `command` string** already exists in the array.
3. If it does not exist, append it to the array. If it already exists, skip (idempotent).

This ensures existing non-handoff hooks are preserved and handoff hooks are never duplicated.

Note which event types need a new array created vs. which need entries appended. Apply these edits only in the **Apply** phase below.

## After setup

### Summary and confirmation

Print the summary as a markdown table (redact secrets — show only last 4 chars). Example:

| Field | Value |
|-------|-------|
| worker_url | https://lark-reply-webhook.example.workers.dev |
| worker_api_key | ***Th_c |
| app_id | cli_a901543264b9ded1 |
| app_secret | ***TbOm |
| email | name@gmail.com |
| hooks | settings.local.json |

Use **AskUserQuestion** with two options: "Apply" (description: "Save all settings and run preflight") and "Cancel" (description: "Discard changes and exit setup").

### Apply

If the user confirms, save all collected values at once.

Write target: `~/.handoff/config.json`

Apply example:

```bash
python3 -c "
import sys, os
for p in ['.claude/skills/handoff/scripts', os.path.expanduser('~/.claude/skills/handoff/scripts')]:
    if os.path.exists(p):
        sys.path.insert(0, p)
        break
import lark_im
lark_im.save_credentials(
    worker_url='<WORKER_URL>',
    worker_api_key='<WORKER_API_KEY>',
    app_id='<APP_ID>',
    app_secret='<APP_SECRET>',
    email='<EMAIL>',
)
"
```

Apply hooks to the chosen settings file (Step 4) using the Edit tool.

Then re-run the preflight check to confirm everything passes.

If preflight fails with "Worker VERIFY_TOKEN not configured", the worker's webhook authentication is not set up. Ask the user to provide the **Verification Token** from their Lark app console (**Event Subscriptions** → **Encryption Strategy**). Then store it as a worker secret:
```bash
echo '<VERIFICATION_TOKEN>' | npx wrangler secret put VERIFY_TOKEN --config .claude/skills/handoff/worker/wrangler.toml
```
Re-run preflight to confirm it passes. This step is required — the worker rejects all webhook events without a valid VERIFY_TOKEN.

### Completion message

**Keep it short.** After preflight passes, print exactly two lines:

```
Setup complete. Exit and restart <tool>, then run /handoff.
```

Where `<tool>` is "Claude Code" or "OpenCode" depending on the runtime.

Always include the restart instruction — even if hooks already existed. Restarting is always safe and ensures hooks are active.

Do **not** summarize what was configured, explain what each step did, or repeat values already shown in the summary table.
