---
name: slay-spire-bridge
description: Use when Codex needs to inspect or control Slay the Spire through the local bridge HTTP API, decide the next move, submit play/choose/proceed/end commands, or debug agent_bridge.py at 127.0.0.1.
---

# Slay Spire Bridge

Project docs:

```text
README.md
```

## Core Rule

Codex is the decision layer. `agent_bridge.py` only exposes state, logs, the web UI, and a command queue.

Do not use bridge-local AI/autopilot behavior. Keep the bridge in `manual` when Codex controls the game. Never write to CommunicationMod stdin/stdout directly.

## Base URL

Default:

```text
http://127.0.0.1:8787
```

If `run/server.txt` exists in the project directory, read it first and use that URL.

## Control Loop

1. Read `/api/codex_context` as raw text.
2. Choose exactly one command from `suggested_commands`.
3. Submit it through `POST /api/command` with `source: "codex"` and a short reason.
4. Re-read `/api/codex_context` before the next decision.

Never run a local combat autoplayer, batch script, or loop that submits multiple combat decisions from one stale state. Helper scripts are only acceptable for read-only summaries or low-risk non-combat screens, and must stop immediately when combat starts or HP safety changes.

For analysis-only requests, do not submit a command. Explain the recommended command instead.

PowerShell:

```powershell
(Invoke-WebRequest -Uri 'http://127.0.0.1:8787/api/codex_context' -TimeoutSec 5).Content
```

Do not pipe `Invoke-RestMethod` through `ConvertTo-Json -Depth 120`; PowerShell can fail locally before Codex sees the JSON.

Submit:

```http
POST /api/command
Content-Type: application/json

{"command":"play 1 0","source":"codex","reason":"Short reason."}
```

## Useful Endpoints

Normal play:

```http
GET /api/codex_context
GET /api/commands
GET /api/summary
POST /api/command
POST /api/mode
```

Debug:

```http
GET /api/debug
GET /api/logs?limit=80
GET /api/state
```

Prefer `/api/codex_context` for decisions. Use `/api/commands` if you only need current command suggestions. Use `/api/state` only when compact context is missing a necessary raw field.

## Decision Rules

Use `suggested_commands`, not raw `available_commands`. The suggestions contain concrete indexes, targets, screen purpose, and safety hints.

General priorities:

- Combat: lethal first, then prevent meaningful HP loss, then spend energy on efficient damage/draw/scaling.
- Critical combat mode: if `estimated_hp_loss > 0`, HP is at or below 50%, incoming damage can kill, damage is over 30% max HP, multiple enemies attack, or Frail/Vulnerable/Confusion/Snecko is active, manually compare all legal block, lethal, and potion lines before offense or `end`.
- Status-heavy boss fights: when Burn/Wound/status density is high, especially against Hexaghost, treat locked hands as the main loss condition. Prefer exhaust/draw and faster lethal setup; avoid optional status generation unless it prevents immediate lethal damage.
- Fairy in a Bottle is not spendable HP. Use it only as a backup when every safer legal line is worse; do not choose a line because Fairy can rescue it.
- Do not end turn while useful playable cards remain unless energy is gone or playing them is harmful.
- Potions: use for lethal, boss/elite swing turns, or preventing major HP loss. Discard only when suggested.
- Card rewards: take premium cards or real deck needs; skip mediocre cards when `return` is suggested. For Defect, high-priority cards include Glacier, Defragment, Electrodynamics, Ball Lightning, Cold Snap, Coolheaded, Charge Battery, Leap, Hologram, Chill, and Skim.
- Rest sites: smith when HP is safe; rest when survival is threatened. Do not upgrade starter `Strike`/`Defend` unless there is no meaningful alternative; prefer class/core cards and run-defining upgrades.
- Map: balance growth with HP safety; avoid elites at low HP or with a weak deck.
- Shop: remove bad starters and buy high-impact cards/relics; avoid marginal purchases.
- Events: avoid large HP loss unless the reward clearly improves win chance.

Tactical playbook:

- Sequence first: play draw before spending energy, but play draw cards before Battle Trance/No Draw effects. Use Shrug It Off before Battle Trance when both are available.
- Count the full turn cost: include enemy attack, end-of-turn Burn damage, Beat of Death, curses/status effects, and self-damage before choosing offense.
- Prefer exact survival over overblocking. If already safe, spend remaining energy on damage, Vulnerable, Weak, Strength reduction, or scaling.
- Exhaust deliberately. True Grit+/Second Wind should remove curses, Burn, Wound, Slimed, or dead starters before valuable attacks/block.
- Race when defense is not scaling. If the deck cannot block future turns, prioritize Bash/Vulnerable, premium attacks, potions, and lethal setup over small extra block.
- Do not create status cards casually. Power Through, Wild Strike, Reckless Charge, and similar cards are good only when their immediate block/damage outweighs future draw damage.
- Potion timing matters: use attack/fire/explosive potions to end elite/boss fights before a dangerous reshuffle; use block/dex potions before damage happens, not after.

Combat Technique Additions:

- Lethal-risk turn: if incoming damage can kill, audit in this order: exact lethal, Weak/Strength reduction, deterministic block/orb evokes, Hologram recursion, then draw. Do not spend the only energy on low-value damage before checking survival outs.
- Draw sequencing under danger: draw first only when it can reveal playable or zero-cost survival/lethal outs; otherwise play known Weak/block first.
- Defect orb math: count current block, immediate Frost evokes, and end-turn Frost passive separately. Do not evoke the last Frost for damage if it makes the next attack lethal.
- Hologram targets: on dangerous turns prefer Glacier, Steam Barrier+, Go for the Eyes, Charge Battery, or a draw card that can be played immediately. On safe turns use it for burst only when it accelerates lethal.
- Boss phase triggers: avoid pushing Slime Boss, Guardian, or Champ through a dangerous threshold unless the current and next turn are survivable or the fight can end quickly.
- Champ: before half HP, build/preserve a defensive answer for Execute. After half HP, treat 12+ Strength or multi-hit intent as lethal pressure; prioritize Weak, Glacier, Hologram recursion, and fast lethal over chip damage. Do not spend all potions before half unless that line also survives the post-half attack cycle.
- Potion economy: keep at least one boss-turn answer when possible. Use potions earlier only if the alternative is major HP loss or losing the fight before the boss phase.

Act and boss heuristics:

- Act 1 needs frontloaded damage before elites. Add real attacks before taking extra skills; Gremlin Nob punishes skill-heavy decks.
- Nob: kill quickly, play only high-value skills, and accept small damage if it saves a turn.
- Lagavulin: use sleep turns for setup, then apply debuffs/scaling before the first attack cycle if available.
- Sentries: kill one outer Sentry fast; AoE and exhaust/draw are premium because Dazed can lock hands.
- Slime Boss: avoid splitting just above half unless the next hit creates a strong split; prepare burst before the split turn.
- Guardian: plan around mode shift; do not take unnecessary thorns damage from low-value attacks.
- Hexaghost: first attack scales from entry HP, but after the opener the fight is a race against Burn density. Avoid long fights and value exhaust/draw highly.

Ironclad priorities:

- Early premium commons/uncommons include Shrug It Off, Pommel Strike, Clothesline, Anger, Battle Trance, Shockwave, Flame Barrier, Offering, Feel No Pain, Evolve, and strong AoE.
- Bash+ is usually a high-impact Act 1 upgrade. Upgrade True Grit when exhaust targeting matters. Avoid Strike/Defend upgrades unless no meaningful card exists.
- Remove bad cards when possible; remove curses first, then usually Strike before Defend unless the deck lacks damage.
- Exhaust synergy is strong only when it has payoffs or solves hand quality. Do not add too many Wound/Burn generators without Evolve, Feel No Pain, Dark Embrace, or enough draw.

Screen-specific rules:

- `screen.purpose=shop_entrance`: choose the shop entry command first; do not infer merchandise until `SHOP_SCREEN`.
- `screen.purpose=smith_upgrade`: choose one high-impact upgrade, then prefer `proceed`/confirm over `return`.
- `screen.type=HAND_SELECT`: choose the requested card carefully; if selected cards are shown and `proceed` is suggested, confirm the selection.
- `screen.type=CARD_REWARD` during combat/potion effects: choose the best offered card when `choose N` is suggested; do not wait for extra expansion if cards and commands are already present.
- `screen.type=GAME_OVER` or `screen.name=DEATH`: the current run is terminal. Do not issue combat commands or try to roll back; use only terminal `suggested_commands`.
- Startup with `in_game=false`: continue/resume if exposed. Do not start a new run unless the user explicitly asks or no local autosave exists.

## Safety

Only submit one command at a time. Observe the next state before deciding again.

After submitting a non-utility command (`play`, `end`, `potion`, `choose`, `proceed`), the next decision must be based on a fresh state. If the command appears queued but HP, hand, energy, screen, and turn are unchanged, wait 2-3 seconds or re-read state; do not repeat the same command blindly.

If `action_phase=EXECUTING_ACTIONS`, normally wait or re-read state. Exception: when an interactive selection screen such as `HAND_SELECT` or `CARD_REWARD` has concrete `suggested_commands` like `choose N` or `proceed`, that command is valid.

If uncertain, re-read `/api/codex_context` or explain what information is missing.

## Debugging

Useful files under the project `run/` directory:

```text
latest_summary.txt
latest_state.json
states.jsonl
commands.jsonl
events.jsonl
errors.jsonl
agent.log
server.txt
```

Common checks:

- API unavailable: read `server.txt`, then check the game process and `agent.log`.
- Command rejected: re-read `/api/commands`; the command must match a current suggestion.
- Command queued but not sent: check `/api/debug` queue size and whether new states are arriving.
- Repeated `wait 30`: often means the game is executing actions; check `screen.type`, `action_phase`, and `suggested_commands`.
- Python changes require restarting the bridge/game process before they take effect.
