---
name: session-pilot
description: Per-chat memory, project instruction onboarding, and task router. Use at the start of every conversation and on each new user prompt to recall where this chat left off, pick the best installed skill/agent/plugin for the task, align with imported project instructions, keep token use low, mirror the user's tone, and checkpoint progress so the next message (or a resumed chat) continues seamlessly. Skip routing for small talk and simple questions.
license: MIT
metadata:
  version: "1.2.0"
---

# Session Pilot

Lightweight memory + router. Budget: **at most 2 helper calls before real work**
(`recall`, then `find` only if it's a task). Never browse skill folders by hand —
the helper keeps a cached index of every skill, agent and plugin on the machine.

`SP` below means: `python <this skill's folder>/scripts/sp.py` (Python 3.8+, no deps;
use `python3` if that is your interpreter).

## Session id

Pass `--session <id>` on every call. Use the most stable id your client exposes:
the conversation/thread/chat id if you know it, otherwise a short slug you choose
on the first turn and keep for the whole chat (e.g. `2026-10-28-fastapi-auth`).
If you can't hold an id, omit the flag: memory is then per-project (cwd).

## Each user turn

1. **Recall (first turn of a chat, or after a context reset/compaction):**
   `SP recall --session <id>`. `NEW SESSION` means a fresh start. Otherwise
   continue from the last `next:` item — don't re-ask what memory already answers.
   Skip recall when the conversation history is still in your context.
   *Note: If the underlying chat or project directory was deleted, `recall` automatically purges orphaned memory.*

2. **Triage the prompt in your head (no tool call):**
   - *Chat / simple question / opinion* → answer directly in lean mode. Done.
   - *Task* (build, fix, analyze, write, research, automate, multi-step) → step 3.

3. **Route (tasks only):** condense the task to 4–10 keywords, then
   `SP find "<keywords>" -k 5`. Read the `match:` line:
   - `strong` → read **only** result #1's SKILL.md (path given) and follow it.
     Results within ~15% of #1's score that cover the same job are alternatives;
     use #2 only if #1 doesn't fit after reading it. Pull in a second skill only
     when the task clearly spans two domains.
   - `weak` → if #1's description genuinely fits, use it; otherwise treat as `none`.
   - `none` → ask once: *"No installed skill covers this well. Want me to search
     for and install one (marketplace / skills.sh / GitHub)?"* If yes, search,
     propose the best 1–2 with source links, install only after approval, then
     `SP index`. If no, use the closest local match from the list, or your own
     general ability, and say which.

4. **Checkpoint** after each meaningful step and before your final reply:
   `SP save --session <id> --done "<what finished>" --next "<next step>" [--skill <name>]`
   On the first save also pass `--title "<3–6 words>" --goal "<one line>"`.
   Mark `--status done` when the goal is complete. There is no reliable
   "chat closed" event, so the latest checkpoint *is* the saved end state.

## Project Import & Skill Reconciliation

When opening, cloning, or importing a project:
1. **Import instructions:** Run `SP import-project [PATH]`.
   - Surfaces existing guidelines (`AGENTS.md`, `CLAUDE.md`, `GEMINI.md`, `.cursorrules`, `README.md`) so you immediately implement the project's instructions.
   - Wires Session Pilot bootstrap rules into the project so memory and routing remain active.
2. **Reconcile project-local skills:** Run `SP reconcile [PATH] [--adopt]`.
   - If the project includes local skills (in `skills/`, `.gemini/skills/`, `.cursor/skills/`, etc.):
     - Compares capability against installed system skills.
     - If an installed system skill is more capable: implements the system skill.
     - If no system skill fits or the project skill has custom domain rules: **adopts the project skill** into system skills so all agents have access.

## Automatic Memory Lifecycle (Deletions & GC)

- **Chat deletion:** If a user deletes a chat/conversation, its memory is automatically purged upon `recall` or by the background daemon.
- **Project deletion:** If a user deletes an entire project folder, all session checkpoints associated with that project are automatically garbage collected (`SP gc`).
- **Explicit project purge:** Run `SP forget-project [PATH]` to clear all saved memory for a given directory.

## Lean mode (always on)

- Answer the question asked; no preamble, no restating the prompt, no recap of
  tool output — quote only the lines that matter.
- Read files in targeted ranges; never re-read what's already in context.
- Prefer one combined command over several small ones.
- For long sessions, if `SP optimizers` lists a context/compaction skill
  (e.g. `strategic-compact`), load it **once per session**, not per message.
  Check at most once per session; record the result with `--skill`.

## Mirror the user's style

Match their language, length, formality, formatting (bullets vs prose), and
emoji use. Once it's clear, save it: `SP save --session <id> --style "<e.g.
casual, Bangla-English mix, short bullets, no emoji>"`. Add `--global-style` if
it's how they talk everywhere — new chats then start with it. Update only when it
changes.

## Rules

- Don't announce the routine ("Recalling memory…"). Mention the chosen skill in
  one short clause only when it affects the result.
- Never install anything, or send data outside the machine, without the user's yes.
- Memory holds summaries, never secrets: no keys, tokens, passwords, or personal data.
- If Python or shell access is unavailable, skip the helper: keep a 3-line
  `Goal / Done / Next` state in your replies instead. See
  [references/clients.md](references/clients.md).

## Helper reference

| Command | Purpose |
| --- | --- |
| `recall --session ID [--last N]` | Compact memory for this chat |
| `save --session ID --done .. --next .. [--title --goal --skill --style --status --global-style]` | Checkpoint |
| `find "keywords" [-k 5] [--kind skill\|agent\|plugin]` | Ranked capabilities |
| `import-project [PATH] [--adopt]` | Scan instructions, wire rules & reconcile |
| `reconcile [PATH] [--adopt]` | Compare project vs system skills (adopt if none fits) |
| `gc [--days N]` | Purge memory of deleted chats/projects |
| `forget-project [PATH]` | Delete all memory for a project folder |
| `optimizers` | Installed context/token-saving skills |
| `index [--rebuild]` / `roots` | Refresh index / show scanned folders |
| `sessions` · `forget ID` · `prune --days 90` | Manage memory |
| `style set "..."` · `prefer <skill>` | Global default style / pinned optimizer |
| `daemon [start\|stop\|status]` | Background watcher & auto-GC service |
