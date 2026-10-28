<!-- session-pilot:start -->
## Session Pilot (always on)

At the start of every chat, and on each new prompt, follow the `session-pilot` skill:
recall this chat's memory, answer simple questions directly in lean mode, route
tasks to the best installed skill (`sp.py find`), mirror the user's tone, and
checkpoint progress (`sp.py save`) after meaningful steps. Load the skill's
SKILL.md once per chat. Don't re-read it on every turn.
<!-- session-pilot:end -->
