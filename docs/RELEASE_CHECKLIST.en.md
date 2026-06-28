# Release Checklist

English | [中文](RELEASE_CHECKLIST.zh.md)

Before publishing to a public repository, check at least the following items.

## Sensitive Information

- [ ] `run/` is not committed.
- [ ] `llm_config.local.json` is not committed.
- [ ] `.env` or other local config files are not committed.
- [ ] No real API keys, tokens, cookies, or Authorization headers are present.
- [ ] No personal usernames, local absolute paths, or temporary directory paths are present.

Recommended scan:

```powershell
rg -n "sk-[A-Za-z0-9_-]{20,}|Bearer [A-Za-z0-9_.-]{20,}|\\.env" .
```

## Documentation

- [ ] README explains the project purpose, install flow, startup flow, and common APIs.
- [ ] `docs/INSTALL_MODS.zh.md` and `docs/INSTALL_MODS.en.md` explain ModTheSpire, BaseMod, and Communication Mod setup.
- [ ] `docs/AGENT_USAGE.zh.md` and `docs/AGENT_USAGE.en.md` explain how Codex or another agent should use the bridge.
- [ ] Skill documentation matches HTTP API behavior.

## Code

- [ ] `python -m py_compile agent_bridge.py` passes.
- [ ] `python agent_bridge.py --self-test` passes.
- [ ] stdout is used only for CommunicationMod protocol output.
- [ ] The local HTTP server listens on `127.0.0.1` by default.
- [ ] External LLM API calls are disabled by default.

## Release Notes

- [ ] License file exists.
- [ ] README feature notes are up to date.
- [ ] If a real secret ever appeared in Git history, rotate the secret first, then clean the history.
