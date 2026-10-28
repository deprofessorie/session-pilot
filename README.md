# Session Pilot

**Per-chat memory, project instruction onboarding, and task router for AI coding agents.** Session Pilot remembers where
each chat left off. It picks the best installed skill, agent or plugin for each task,
keeps answers lean, implements imported project instructions, and adopts your tone.

Works with any client that loads `SKILL.md` skills: Claude Code, Antigravity, Gemini CLI,
Codex CLI, OpenCode, Cursor and GitHub Copilot. A reduced mode works in chat apps with
no shell. Needs only Python 3.8+, with no packages to install.

```
you: "add rate limiting to the API"
        │
        ├─ recall   → "SESSION 2026-10-28-api | next: write token refresh tests"
        ├─ triage   → task (not small talk)
        ├─ find     → 1. fastapi-patterns  2. backend-patterns  3. agency-backend-architect
        ├─ load #1 only, do the work, in your tone
        └─ save     → done: rate limiter added | next: load test
```

## What it does

| Feature | How |
| --- | --- |
| **Per-chat memory** | Each chat gets a small JSON record (goal, style, skills used, last 8 checkpoints, older steps compressed to one line). Resuming a chat prints about 5 lines, not the whole history. |
| **Auto-GC on Deletions** | If you delete a chat or an entire project directory, Session Pilot automatically cleans up orphaned memory upon recall or via the background daemon (`sp.py gc`). |
| **Project Import & Rules** | When importing or opening a project, `sp.py import-project` scans and surfaces all project rules (`AGENTS.md`, `CLAUDE.md`, `.cursorrules`, etc.) and wires Session Pilot into the project. |
| **Project vs System Skills** | `sp.py reconcile` compares project-local skills against installed system skills. If a system skill is more capable, it implements it; if no system skill fits or the project skill has domain rules, it **adopts the project skill** into your system tools. |
| **Background Indexer** | `sp.py daemon start` runs a background watcher that pre-warms the index and watches skill/plugin folders. When you install or clone a new skill, it indexes it in seconds and logs it to `changelog.json`. |
| **Session Tool Awareness** | Resuming or continuing an existing conversation alerts the agent if new skills were installed since the last checkpoint, making newly added tools available immediately. |
| **Skill routing** | `sp.py find` searches the pre-warmed index of every skill, agent and plugin on the machine, ranks them with BM25, and returns the top 5 with a `strong / weak / none` verdict. Instant response (<50 ms). |
| **Tie-breaking** | When several skills match, project-local beats global, then the higher score wins. Duplicate names are collapsed. Only #1 gets loaded. |
| **Missing skills** | On `none`, it asks before searching online or installing anything. If you say no, it falls back to the closest local skill or general ability. |
| **Lean mode** | Small talk and simple questions skip routing entirely. Always on: no preamble, targeted file reads, no restating tool output. An installed context/compaction skill is loaded at most once per session. |
| **Tone mirroring** | Matches your language, length, formality, formatting and emoji use. Saves it per chat, or globally with `--global-style`. |

### Token cost

- `SKILL.md` is about 1,200 tokens, loaded once per chat.
- Helper output is about 60–200 tokens per call.
- At most two calls happen before real work (`recall`, `find`), and simple questions make none.

## Install

```bash
git clone https://github.com/deprofessorie/session-pilot.git
cd session-pilot

python install.py --client antigravity --with-rule   # Antigravity (global skill + always-on plugin rule)
python install.py --client claude --with-rule        # Claude Code (global)
python install.py --client codex --with-rule         # Codex CLI
python install.py --client gemini --with-rule        # Gemini CLI
python install.py --client cursor --project . --with-rule
python install.py --client copilot --project . --with-rule
python install.py --client all --dry-run             # preview every detected client
```

`--with-rule` automatically installs the always-on bootstrap rule for your client so you don't need to manually paste anything into project files. For Antigravity, it registers an enabled global plugin with rules in `~/.gemini/config/` so Session Pilot runs in every chat even if you have hundreds of other skills installed. The rule is wrapped in markers, so reinstalling won't duplicate it and `--uninstall` removes it cleanly.

Manual install: copy `skills/session-pilot/` into your client's skills folder. Per-client
paths are listed in [`references/clients.md`](skills/session-pilot/references/clients.md).

## Helper CLI

```bash
SP="python skills/session-pilot/scripts/sp.py"

# Memory & Lifecycle
$SP recall --session my-chat               # what this chat remembers (+ newly installed tools)
$SP save --session my-chat --done "auth"   # save checkpoint
$SP gc                                     # purge memory of deleted chats/projects
$SP forget my-chat                         # delete single chat memory
$SP forget-project /path/to/project        # delete all memory for a project folder
$SP sessions                               # recent chats

# Project Onboarding & Skill Reconciliation
$SP import-project /path/to/project        # read rules, wire session-pilot & reconcile skills
$SP reconcile /path/to/project [--adopt]   # compare project skills vs system skills (adopt if none fits)

# Skill Search & Background Daemon
$SP find "review react component a11y"     # ranked matches (<50ms)
$SP index                                  # build/refresh the capability index
$SP daemon start                           # start background indexer & auto-GC
$SP daemon status                          # check if background watcher is running
$SP daemon stop                            # stop background indexer
$SP changelog                              # see recently added/removed skills
$SP optimizers                             # installed context/token-saving skills
$SP style set "casual, short bullets"      # default tone for new chats
$SP prefer strategic-compact               # pin an optimizer skill
```

## Where things live

| Path | Contents |
| --- | --- |
| `~/.session-pilot/sessions/<id>.json` | One file per chat (stores project cwd, conv id, goal, checkpoints) |
| `~/.session-pilot/index.json` | Cached skill index, refreshed when skill folders change or every 24 h |
| `~/.session-pilot/profile.json` | Global style and pinned optimizer |
| `~/.session-pilot/changelog.json` | Skill additions and removals log |

Override the location with `SESSION_PILOT_HOME`. Memory stays on your machine and holds
short summaries only. The skill tells the model never to store secrets.

Scanned folders cover the default skill, agent and plugin directories of the clients above,
plus project folders (`.agents/skills`, `.claude/skills`, `skills/`, …) from the current directory up
to the repo root. Add more with `SESSION_PILOT_ROOTS`. Run `sp.py roots` to see the list.

## Limits

- **No "chat closed" event.** Clients don't signal when a chat ends, so the agent saves
  a checkpoint after each meaningful step. The latest checkpoint is the end state.
- **Session identity depends on the client.** If the model can see a conversation id, it
  uses that. Otherwise it picks a slug on the first turn and keeps it. With no id at all,
  memory is per project folder. See [clients.md](skills/session-pilot/references/clients.md).
