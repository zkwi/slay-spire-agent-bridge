# Slay the Spire Agent Bridge

Local HTTP bridge for controlling Slay the Spire through CommunicationMod.

本项目是一个本地 HTTP 桥接器，用于通过 CommunicationMod 控制《杀戮尖塔》。

![Codex controlling Slay the Spire through the bridge](docs/assets/codex-gameplay.png)

The screenshot shows Codex reading game state, reasoning about the next command, and sending actions through the local bridge.

## Documentation

- [English README](README.en.md)
- [中文 README](README.zh.md)

Detailed docs:

- [Install Mods / 安装 Mod](docs/INSTALL_MODS.md)
- [Agent Usage / Agent 使用](docs/AGENT_USAGE.md)
- [Release Checklist / 发布检查清单](docs/RELEASE_CHECKLIST.md)
- [Contributing / 贡献说明](CONTRIBUTING.md)
- [Security / 安全说明](SECURITY.md)

## Quick Links

- Local web UI: `http://127.0.0.1:8787`
- Main bridge: `agent_bridge.py`
- Codex skill: `skills/slay-spire-bridge/SKILL.md`
- Example LLM config: `llm_config.example.json`

## License

MIT License. See [LICENSE](LICENSE).
