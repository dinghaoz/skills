# skills

Agent skills for Claude Code and OpenCode.

## Install

From your project root (installs into `.claude/skills/`):

```bash
npx skills add dinghaoz/skills
```

Or install a specific skill:

```bash
npx skills add dinghaoz/skills/handoff
npx skills add dinghaoz/skills/lark-wiki
```

> **Note:** Both skills support global install (`-g`). Install `handoff` per-project if you want the hook config committed to git alongside your project. Install globally if you prefer a single setup across all projects.

## Skills

### [handoff](./handoff/)

Hand off your CLI session to Lark so you can continue interacting with Claude from your phone. Supports both Claude Code and OpenCode.

### [lark-wiki](./lark-wiki/)

Read, create, and edit Lark wiki pages and documents via the Lark Open API.

## License

MIT
