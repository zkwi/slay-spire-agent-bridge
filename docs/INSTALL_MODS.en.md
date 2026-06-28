# Install Mods and Configure CommunicationMod

English | [中文](INSTALL_MODS.zh.md)

This document covers the game-side setup required to run this project.

## Requirements

Required:

```text
Slay the Spire
ModTheSpire
BaseMod
Communication Mod
Python environment
agent_bridge.py
```

The Communication Mod Steam Workshop page lists `ModTheSpire` and `BaseMod` as required items. The CommunicationMod GitHub README also lists Slay the Spire, ModTheSpire, and BaseMod as requirements.

The BaseMod GitHub README notes that normal usage requires Java 8 and ModTheSpire. Steam installations usually handle Java through the Slay the Spire / ModTheSpire environment; manual installs may need more attention.

Optional but recommended:

```text
Enable Fast Mode in game settings
```

CommunicationMod notes that it has not been tested with fast mode disabled. This is a game setting recommendation, not an extra mod.

## Recommended Install: Steam Workshop

Subscribe to:

1. [ModTheSpire](https://steamcommunity.com/workshop/filedetails/?id=1605060445)
2. [BaseMod](https://github.com/daviscook477/BaseMod), or the BaseMod Steam Workshop item
3. [Communication Mod](https://steamcommunity.com/workshop/filedetails/?id=2131373661)

Then start Slay the Spire from Steam and choose:

```text
Play With Mods
```

In the ModTheSpire window, enable:

```text
BaseMod
Communication Mod
```

Communication Mod does not play the game by itself. It starts an external process and exchanges JSON state and commands through stdin/stdout.

## Manual Install

Manual installation is not recommended unless Steam Workshop is unavailable.

General steps:

1. Install or prepare ModTheSpire.
2. Put `BaseMod.jar` in the mods directory recognized by ModTheSpire.
3. Put `CommunicationMod.jar` in the same mods directory.
4. Run ModTheSpire and enable BaseMod and CommunicationMod.

The mods directory depends on your ModTheSpire setup. Common locations include:

```text
<SlayTheSpire>\mods
%LOCALAPPDATA%\ModTheSpire\mods
```

If unsure, use the path shown by the ModTheSpire launcher or logs.

## Configure CommunicationMod to Start This Project

CommunicationMod creates its config directory after first launch. The config file is usually:

```text
%LOCALAPPDATA%\ModTheSpire\CommunicationMod\config.properties
```

Recommended config:

```properties
verbose=true
maxInitializationTimeout=30
command=<python.exe> <project>\\agent_bridge.py
runAtGameStart=true
```

Windows example:

```properties
command=C\:\\Path\\To\\Python\\python.exe C\:\\Path\\To\\slay-spire-agent\\agent_bridge.py
```

Notes:

- `command` must point to the Python interpreter and `agent_bridge.py`.
- In Java properties files, Windows `\` usually needs to be written as `\\`, and drive colons may be written as `C\:`.
- Paths with spaces are easier to misconfigure; paths without spaces are recommended.
- Python changes are not hot-reloaded by an already running bridge. Restart the game, ModTheSpire, or bridge process.

## Protocol Notes

CommunicationMod starts the external process and waits for the process to print this line to stdout:

```text
ready
```

After that, it sends stable game state JSON to the external process stdin and waits for commands on stdout.

This project therefore has one hard rule:

```text
stdout is only for CommunicationMod protocol commands
debug output must go to log files, never print to stdout
```

Common commands include:

```text
play 1 0
choose 1
proceed
return
end
wait 30
state
```

## Verify Startup

After starting Modded Slay the Spire, check:

```text
<project>\run\server.txt
<project>\run\agent.log
<project>\run\latest_summary.txt
```

Open in a browser:

```text
http://127.0.0.1:8787
```

Or use PowerShell:

```powershell
(Invoke-WebRequest -Uri 'http://127.0.0.1:8787/api/summary' -TimeoutSec 5).Content
```

## Troubleshooting

`No state received yet`

- The bridge HTTP server started, but CommunicationMod has not sent game state yet.
- Make sure the game was started through ModTheSpire with Communication Mod enabled.

`communication_mod_errors.log` has content

- Check Python path, script path, ready timeout, and stack traces first.
- CommunicationMod also recommends this file for debugging external process startup problems.

Command queued but the game does not move

- Check `run/commands.jsonl` and see whether the command moved from `queued` to `sent`.
- Check `/api/debug` for queue size and the latest `action_phase`.
- If you just changed Python code, restart the bridge.

Main menu cannot automatically Continue

- CommunicationMod may not expose a Continue protocol command.
- If a local autosave exists but no Continue choice is exposed, the bridge blocks automatic `start`; click Continue manually in the game.
