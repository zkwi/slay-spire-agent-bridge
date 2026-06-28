# Using Codex or Other AI Agents

English | [中文](AGENT_USAGE.zh.md)

The agent architecture is intentionally simple:

```text
bridge only stores state and queues commands
AI Agent only makes decisions
all actions go through the HTTP API
```

Do not let an agent write directly to CommunicationMod stdin/stdout.

## Install the Codex Skill

Skill inside this project:

```text
<project>\skills\slay-spire-bridge\SKILL.md
```

Codex skill install location:

```text
%USERPROFILE%\.codex\skills\slay-spire-bridge\SKILL.md
```

To reinstall or sync:

```powershell
Copy-Item -Recurse -Force `
  '.\skills\slay-spire-bridge' `
  "$env:USERPROFILE\.codex\skills\slay-spire-bridge"
```

Validate the skill:

```powershell
$env:PYTHONUTF8='1'
python `
  "$env:USERPROFILE\.codex\skills\.system\skill-creator\scripts\quick_validate.py" `
  "$env:USERPROFILE\.codex\skills\slay-spire-bridge"
```

## Codex Control Loop

Read state:

```powershell
(Invoke-WebRequest -Uri 'http://127.0.0.1:8787/api/codex_context' -TimeoutSec 5).Content
```

Avoid this:

```powershell
Invoke-RestMethod ... | ConvertTo-Json -Depth 120
```

PowerShell can hit local JSON depth limits while re-serializing objects, which prevents the agent from seeing the real JSON.

Submit a command:

```powershell
$body = @{
  command = 'choose 1'
  source = 'codex'
  reason = 'Choose the strongest offered card.'
} | ConvertTo-Json -Compress

Invoke-RestMethod `
  -Uri 'http://127.0.0.1:8787/api/command' `
  -Method Post `
  -ContentType 'application/json' `
  -Body $body
```

Submit exactly one command at a time, then read the state again.

## Other AI Agents

Have the agent read this skill:

```text
<project>\skills\slay-spire-bridge\SKILL.md
```

If the agent does not support skills, include this in its system or developer prompt:

```text
You control Slay the Spire only through http://127.0.0.1:8787.
Read GET /api/codex_context.
Choose exactly one command from suggested_commands.
Submit it with POST /api/command using {"command": "...", "source": "agent", "reason": "..."}.
Re-read context before the next command.
Never write to CommunicationMod stdin/stdout.
Do not use bridge-local AI mode.
```

Python standard library example:

```python
import json
import urllib.request

BASE = "http://127.0.0.1:8787"

context = urllib.request.urlopen(
    BASE + "/api/codex_context",
    timeout=5,
).read().decode("utf-8")

print(context)

payload = json.dumps({
    "command": "end",
    "source": "agent",
    "reason": "No useful playable cards remain.",
}).encode("utf-8")

request = urllib.request.Request(
    BASE + "/api/command",
    data=payload,
    headers={"Content-Type": "application/json"},
    method="POST",
)

print(urllib.request.urlopen(request, timeout=5).read().decode("utf-8"))
```

## Agent Decision Constraints

Required:

- Use `suggested_commands` from `/api/codex_context` or `/api/commands`.
- Submit only one command at a time.
- Re-read state after each command.
- Keep `reason` short and tactical.

Avoid:

- Do not build commands directly from raw `available_commands`.
- Do not submit multiple actions in a row.
- Do not force gameplay commands while mode is `paused`.
- Do not start a new run from the main menu unless the user explicitly asks.
- Do not print debug output to stdout.

## Screen Rules

`COMBAT`

- Take lethal first, then prevent meaningful HP loss, then play efficient damage/draw/vulnerable/scaling cards.

`CARD_REWARD`

- For combat rewards, take premium cards or cards that solve a real deck need; skip mediocre cards.
- For potion or combat effect card choices, choose directly if `suggested_commands` already includes `choose N`.

`HAND_SELECT`

- Inspect `screen.selected_cards` and `screen.selection`.
- Use `choose N` when a card must be selected.
- Confirm with `proceed` once the selection is complete.

`REST` / `GRID`

- Prefer smithing when HP is safe.
- When `screen.purpose=smith_upgrade`, choose a strong upgrade; usually `proceed` after selection.

`SHOP_SCREEN`

- Prioritize removing Strike/bad cards, strong relics, and key cards. Avoid low-impact purchases.

`MAP`

- Take growth when HP and deck strength allow it; reduce elite risk when low on HP or weak.

`EXECUTING_ACTIONS`

- Wait by default.
- Exception: interactive selection screens such as `HAND_SELECT` or `CARD_REWARD` may still accept `choose N` or `proceed` if those commands are suggested.

`GAME_OVER` / `DEATH`

- The current run is over. Combat cannot continue, and the bridge cannot roll back the run.
- Use only terminal commands from `suggested_commands`. If only `wait 30` / `state` are available, wait for the game to expose a command that leaves the death screen, or have a human return to the main menu.
- After returning to the main menu, prefer Continue. Start a new run only when the user explicitly agrees or no resumable save exists.

## Debugging Order

1. `GET /api/codex_context`
2. `GET /api/commands`
3. `GET /api/debug`
4. Check `<project>\run\commands.jsonl`
5. Check `<project>\run\errors.jsonl`
6. Check `<project>\run\agent.log`

Common signals:

- `queued` without `sent`: the bridge has not received a new game state, or execution-phase protection is blocking the command.
- `rejected`: the command is not currently available.
- Repeated `wait 30`: likely animation/execution phase, or an interactive selection screen was not recognized. Check `screen.type` and `suggested_commands`.
- Python changes not taking effect: restart the bridge/game.
