# 安全策略

[English](SECURITY.en.md) | 中文

## 支持范围

当前项目处于早期阶段，安全修复优先覆盖主分支。

## 报告安全问题

如果发现以下问题，请通过私下渠道联系维护者，避免直接公开真实密钥或可复现的泄露数据：

- API Key、token、cookie 或其他凭据泄露。
- 本机用户名、绝对路径、存档路径或运行日志意外进入仓库。
- HTTP API 暴露到非本机网络导致未授权控制游戏。
- 日志或错误信息未正确脱敏。

## 本地安全默认值

- HTTP 服务默认只监听 `127.0.0.1`。
- 运行日志写入 `run/`，该目录默认不提交。
- 模型配置文件 `llm_config.local.json` 默认不提交。
- 默认不启用外部 LLM 调用。

## 发布前检查

发布前请执行：

```powershell
rg -n "sk-[A-Za-z0-9_-]{20,}|Bearer [A-Za-z0-9_.-]{20,}|\\.env" .
python -m py_compile agent_bridge.py
```
