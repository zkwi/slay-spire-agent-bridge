# 发布检查清单

[English](RELEASE_CHECKLIST.en.md) | 中文

发布到公开仓库前，至少检查以下内容。

## 敏感信息

- [ ] 没有提交 `run/`。
- [ ] 没有提交 `llm_config.local.json`。
- [ ] 没有提交 `.env` 或其他本地配置。
- [ ] 没有真实 API Key、token、cookie、Authorization header。
- [ ] 没有个人用户名、本机绝对路径、临时目录路径。

推荐扫描：

```powershell
rg -n "sk-[A-Za-z0-9_-]{20,}|Bearer [A-Za-z0-9_.-]{20,}|\\.env" .
```

## 文档

- [ ] README 能说明项目用途、安装方式、启动方式和常用 API。
- [ ] `docs/INSTALL_MODS.zh.md` 和 `docs/INSTALL_MODS.en.md` 说明 ModTheSpire、BaseMod、Communication Mod 的安装和配置。
- [ ] `docs/AGENT_USAGE.zh.md` 和 `docs/AGENT_USAGE.en.md` 说明 Codex 或其他 Agent 的调用方式。
- [ ] Skill 文档和 HTTP API 行为保持一致。

## 代码

- [ ] `python -m py_compile agent_bridge.py` 通过。
- [ ] `python agent_bridge.py --self-test` 通过。
- [ ] stdout 只用于 CommunicationMod 协议输出。
- [ ] 本地 HTTP 服务默认监听 `127.0.0.1`。
- [ ] 默认不会主动调用外部 LLM API。

## 发布说明

- [ ] 确认 License 文件存在。
- [ ] 更新 README 中的功能说明。
- [ ] 如果 Git 历史中曾出现真实密钥，先轮换密钥，再清理历史。
