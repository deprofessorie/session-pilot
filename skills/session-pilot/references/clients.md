# Client setup and session ids

A skill is a set of instructions; whether it runs on *every* chat depends on the client.
Each client needs two things:

1. **The skill folder** copied to where the client discovers skills.
2. **A bootstrap rule**, a 6-line always-on instruction (see
   [`adapters/bootstrap.md`](../../../adapters/bootstrap.md)). Without it, the skill
   only runs when the model decides it's relevant.

`python install.py --client <name> [--with-rule]` does both (see the README).

| Client | Skill folder (global) | Always-on rule file | Session id to use |
| --- | --- | --- | --- |
| Claude Code | `~/.claude/skills/session-pilot` | `~/.claude/CLAUDE.md` | Session id if visible, else a slug |
| Antigravity | `~/.gemini/config/skills/session-pilot` | Global or workspace `GEMINI.md` / `AGENTS.md` | Conversation ID (shown in the system context) |
| Gemini CLI | `~/.gemini/skills/session-pilot` | `~/.gemini/GEMINI.md` | Slug |
| Codex CLI | `~/.codex/skills/session-pilot` | `~/.codex/AGENTS.md` | Slug |
| OpenCode | `~/.config/opencode/skills/session-pilot` | `AGENTS.md` | Slug |
| Cursor | `<project>/.cursor/skills/session-pilot` | `.cursor/rules/session-pilot.mdc` (provided) | Slug |
| GitHub Copilot | `<project>/.github/skills/session-pilot` | `.github/copilot-instructions.md` | Slug |
| Any project | `<project>/.agents/skills/session-pilot` | `<project>/AGENTS.md` | Slug |

Paths change between client versions. If the client can't see the skill, check its
docs for the current skills directory and copy the folder there.

## Why a slug?

Most clients don't tell the model which chat window it's in. So the model picks a
short id on the first turn (e.g. `2026-10-28-fastapi-auth`) and reuses it. Within a
chat the id stays in the model's context. After a context reset, the model runs
`sp.py sessions`, matches the topic, and resumes with that id. With no id at all,
memory falls back to one record per project folder (`auto-<hash of cwd>`).

You can also pin an id from outside: `SESSION_PILOT_ID=my-chat` in the environment.

## Chat apps without a shell (ChatGPT, Claude.ai, Gemini web)

These can't run `sp.py`. Paste `SKILL.md` into the project/custom instructions. The
model then follows triage, lean mode and style mirroring, and keeps a short state
block in its replies:

```
Goal: …   Done: …   Next: …
```

Persistent memory and the skill index need a local agent that can run Python.

## Environment variables

| Variable | Effect |
| --- | --- |
| `SESSION_PILOT_HOME` | Where memory and the index live (default `~/.session-pilot`) |
| `SESSION_PILOT_ID` | Fixed session id when `--session` is omitted |
| `SESSION_PILOT_ROOTS` | Extra folders to scan, `os.pathsep`-separated; prefix `agent=` or `plugin=` to set the kind |
| `SESSION_PILOT_NO_DEFAULT_ROOTS` | Set to `1` to scan only `SESSION_PILOT_ROOTS` |
