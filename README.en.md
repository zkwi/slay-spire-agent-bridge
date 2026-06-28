# Slay the Spire Agent Bridge

English | [中文](README.zh.md)

This project is a local bridge that wraps the Slay the Spire CommunicationMod protocol into an HTTP API that is easier for humans and AI agents to use.

> This is an unofficial personal project. It is not affiliated with Slay the Spire, Mega Crit, ModTheSpire, BaseMod, or CommunicationMod.

![Codex controlling Slay the Spire through the bridge](docs/assets/codex-gameplay.png)

The screenshot shows Codex reading game state, reasoning about the next command, and sending actions through the local bridge.

Core flow:

```text
Slay the Spire
  -> ModTheSpire + BaseMod + CommunicationMod
  -> agent_bridge.py stdin/stdout protocol
  -> http://127.0.0.1:8787
  -> Codex / other AI agent / web controller
```

## Project Scope

- CommunicationMod handles communication with the game.
- `agent_bridge.py` stores state, generates summaries, exposes HTTP APIs, queues commands, and sends commands back to the game.
- Codex or another AI agent is responsible for decision making.
- The bridge should normally stay in `manual` mode. Local AI autopilot is disabled by default.

## Features

- Reads structured game state through CommunicationMod.
- Exposes a local HTTP API for Codex or other AI agents.
- Generates compact decision context at `/api/codex_context`.
- Provides a local web controller at `http://127.0.0.1:8787`.
- Writes runtime logs and latest state to `run/` for debugging and replay.
- Does not call external model APIs by default. Users must explicitly configure that behavior.

## Requirements

- Windows, macOS, or Linux.
- Python 3.9+. The bridge currently uses only the Python standard library.
- Slay the Spire.
- ModTheSpire, BaseMod, and Communication Mod.

## Main Files

```text
agent_bridge.py                         # Main bridge
skills/slay-spire-bridge/SKILL.md       # Skill for Codex / AI agents
docs/INSTALL_MODS.en.md                 # Mod installation and CommunicationMod config
docs/AGENT_USAGE.en.md                  # Codex and other AI agent usage
run/                                    # Runtime state, logs, and history
llm_config.example.json                 # Optional LLM config example without real secrets
```

`run/` and `llm_config.local.json` are local runtime artifacts and should not be committed.

## Local Config and Privacy

- Do not commit `run/`; it may contain current game state, command history, and debug logs.
- Do not commit `llm_config.local.json`; it may contain model API keys.
- To configure a local model provider, copy `llm_config.example.json` to `llm_config.local.json` and fill in your own values.
- Before publishing, scan for local paths, usernames, API keys, and runtime logs.
- Debug APIs try to show local paths as placeholders such as `<project>`, `%USERPROFILE%`, and `%LOCALAPPDATA%`.

## Quick Start

1. Follow [docs/INSTALL_MODS.en.md](docs/INSTALL_MODS.en.md) to install `ModTheSpire`, `BaseMod`, and `Communication Mod`.
2. Configure CommunicationMod:

```properties
command=<python.exe> <project>\\agent_bridge.py
runAtGameStart=true
maxInitializationTimeout=30
verbose=true
```

Windows paths in `config.properties` usually need escaping, for example:

```properties
command=C\:\\Path\\To\\Python\\python.exe C\:\\Path\\To\\slay-spire-agent\\agent_bridge.py
```

3. Start Slay the Spire from Steam with `Play With Mods`, and enable:

```text
BaseMod
Communication Mod
```

4. After the bridge starts, open:

```text
http://127.0.0.1:8787
```

5. For agent control, follow [docs/AGENT_USAGE.en.md](docs/AGENT_USAGE.en.md): read `/api/codex_context`, then submit `/api/command`.

## Common APIs

```http
GET  /api/codex_context
GET  /api/summary
GET  /api/commands
GET  /api/debug
POST /api/command
POST /api/mode
```

Basic agent loop:

```text
Read /api/codex_context
  -> choose one command from suggested_commands
  -> POST /api/command
  -> wait for state change and read again
```

## Development Checks

```powershell
python -m py_compile agent_bridge.py
```

The full self-test writes files under `run/`. Avoid running it during an active game unless you know what you are doing:

```powershell
python agent_bridge.py --self-test
```

## Contributing and Security

- See [CONTRIBUTING.en.md](CONTRIBUTING.en.md).
- See [SECURITY.en.md](SECURITY.en.md).
- See [docs/RELEASE_CHECKLIST.en.md](docs/RELEASE_CHECKLIST.en.md).

## License

MIT License. See [LICENSE](LICENSE).

## References

- [CommunicationMod GitHub](https://github.com/ForgottenArbiter/CommunicationMod)
- [Communication Mod Steam Workshop](https://steamcommunity.com/workshop/filedetails/?id=2131373661)
- [ModTheSpire Steam Workshop](https://steamcommunity.com/workshop/filedetails/?id=1605060445)
- [BaseMod GitHub](https://github.com/daviscook477/BaseMod)
