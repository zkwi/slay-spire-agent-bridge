# Slay the Spire Agent Bridge

[English](README.en.md) | 中文

本项目是一个本地桥接器，用来把 Slay the Spire 的 CommunicationMod 协议封装成更容易给人类和 AI Agent 使用的 HTTP API。

> 本项目是非官方个人项目，不隶属于 Slay the Spire、Mega Crit、ModTheSpire、BaseMod 或 CommunicationMod。

![Codex 通过本地 bridge 控制 Slay the Spire](docs/assets/codex-gameplay.png)

截图展示了 Codex 读取游戏状态、分析下一条命令，并通过本地 bridge 向游戏发送操作的流程。

核心链路：

```text
Slay the Spire
  -> ModTheSpire + BaseMod + CommunicationMod
  -> agent_bridge.py stdin/stdout protocol
  -> http://127.0.0.1:8787
  -> Codex / other AI agent / web controller
```

## 当前定位

- CommunicationMod 负责和游戏通信。
- `agent_bridge.py` 负责保存状态、生成摘要、提供 HTTP API、排队并下发命令。
- Codex 或其他 AI Agent 负责决策。
- bridge 默认应保持 `manual` 模式，不使用本地 AI 自动驾驶。

## 功能特性

- 通过 CommunicationMod 读取结构化游戏状态。
- 暴露本地 HTTP API，供 Codex 或其他 AI Agent 调用。
- 生成精简决策上下文：`/api/codex_context`。
- 提供本地网页控制台：`http://127.0.0.1:8787`。
- 保存运行日志和最新状态到 `run/`，便于调试和复盘。
- 默认不调用任何外部模型 API；需要时必须由使用者显式配置。

## 环境要求

- Windows、macOS 或 Linux。
- Python 3.9+。当前实现只使用 Python 标准库。
- Slay the Spire。
- ModTheSpire、BaseMod、Communication Mod。

## 主要文件

```text
agent_bridge.py                         # 主桥接器
skills/slay-spire-bridge/SKILL.md       # 给 Codex/AI Agent 使用的 Skill
docs/INSTALL_MODS.zh.md                 # Mod 安装与 CommunicationMod 配置
docs/AGENT_USAGE.zh.md                  # Codex 和其他 AI Agent 调用方式
run/                                    # 运行时状态、日志、历史记录
llm_config.example.json                 # 可选 LLM 配置示例，不含真实密钥
```

`run/` 和 `llm_config.local.json` 是本机运行产物，不应该提交。

## 本地配置与隐私

- 不要提交 `run/`，其中包含当前局面、命令历史和调试日志。
- 不要提交 `llm_config.local.json`，其中可能包含模型 API Key。
- 如需本地模型配置，复制 `llm_config.example.json` 为 `llm_config.local.json` 后再填写自己的配置。
- 发布前建议执行一次敏感信息扫描，确认没有本机路径、用户名、API Key 或运行日志。
- 调试 API 会尽量把本机路径显示为 `<project>`、`%USERPROFILE%`、`%LOCALAPPDATA%` 等占位形式。

## 快速启动

1. 按 [docs/INSTALL_MODS.zh.md](docs/INSTALL_MODS.zh.md) 安装 `ModTheSpire`、`BaseMod`、`Communication Mod`。
2. 配置 CommunicationMod：

```properties
command=<python.exe> <project>\\agent_bridge.py
runAtGameStart=true
maxInitializationTimeout=30
verbose=true
```

Windows 路径写入 `config.properties` 时通常需要转义，例如：

```properties
command=C\:\\Path\\To\\Python\\python.exe C\:\\Path\\To\\slay-spire-agent\\agent_bridge.py
```

3. 从 Steam 启动 `Play With Mods`，勾选：

```text
BaseMod
Communication Mod
```

4. bridge 启动后访问：

```text
http://127.0.0.1:8787
```

5. 给 Agent 使用时，按 [docs/AGENT_USAGE.zh.md](docs/AGENT_USAGE.zh.md) 读取 `/api/codex_context`，再提交 `/api/command`。

## 常用 API

```http
GET  /api/codex_context
GET  /api/summary
GET  /api/commands
GET  /api/debug
POST /api/command
POST /api/mode
```

Agent 的基本循环：

```text
读取 /api/codex_context
  -> 从 suggested_commands 选择一个命令
  -> POST /api/command
  -> 等待状态变化后重新读取
```

## 开发验证

```powershell
python -m py_compile agent_bridge.py
```

完整自测会写入 `run/latest_state.json` 等运行文件，正在跑局时不要随便执行：

```powershell
python agent_bridge.py --self-test
```

## 贡献与安全

- 贡献说明见 [CONTRIBUTING.zh.md](CONTRIBUTING.zh.md)。
- 安全与隐私报告见 [SECURITY.zh.md](SECURITY.zh.md)。
- 开源发布前检查清单见 [docs/RELEASE_CHECKLIST.zh.md](docs/RELEASE_CHECKLIST.zh.md)。

## License

MIT License. See [LICENSE](LICENSE).

## 参考来源

- [CommunicationMod GitHub](https://github.com/ForgottenArbiter/CommunicationMod)
- [Communication Mod Steam Workshop](https://steamcommunity.com/workshop/filedetails/?id=2131373661)
- [ModTheSpire Steam Workshop](https://steamcommunity.com/workshop/filedetails/?id=1605060445)
- [BaseMod GitHub](https://github.com/daviscook477/BaseMod)
