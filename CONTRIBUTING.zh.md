# 贡献说明

[English](CONTRIBUTING.en.md) | 中文

欢迎提交 issue、文档改进和小而清晰的代码修复。

## 开发原则

- 保持实现简单，优先使用 Python 标准库。
- 不提交本地运行产物、游戏状态日志、API Key 或个人路径。
- HTTP API 和 Skill 文档需要保持一致。
- 修改 CommunicationMod 协议输出相关逻辑时，必须确认 stdout 只输出协议命令。
- 文档需要同时维护中文和英文版本。

## 本地检查

```powershell
python -m py_compile agent_bridge.py
python agent_bridge.py --self-test
```

`--self-test` 会生成 `run/` 目录。该目录已被 `.gitignore` 忽略，不应提交。

## Pull Request 建议

- 说明改动目的和影响范围。
- 说明是否改动了 API、Skill 文档或安装流程。
- 贴出本地检查结果。
