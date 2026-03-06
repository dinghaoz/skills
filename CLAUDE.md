# Skills Development Repository

This repository contains custom skills for Claude/OpenCode.

## Structure

- `handoff/` - Handoff skill for continuing conversations via Lark
- `lark-wiki/` - Lark wiki integration skill

## Installing Skills

When the user says "install <skill-name>" (e.g., "install handoff"), run:
```bash
./install-skill.sh <skill-name>
```

This copies the skill from this development repo to `~/.agents/skills/` for use.
