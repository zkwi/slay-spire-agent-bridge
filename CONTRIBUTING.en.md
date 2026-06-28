# Contributing

English | [中文](CONTRIBUTING.zh.md)

Issues, documentation improvements, and small focused code fixes are welcome.

## Development Principles

- Keep the implementation simple and prefer the Python standard library.
- Do not commit local runtime artifacts, game state logs, API keys, or personal paths.
- Keep HTTP API behavior and Skill documentation in sync.
- When changing CommunicationMod protocol output logic, make sure stdout only emits protocol commands.
- Documentation should be maintained in both English and Chinese.

## Local Checks

```powershell
python -m py_compile agent_bridge.py
python agent_bridge.py --self-test
```

`--self-test` creates a `run/` directory. It is ignored by `.gitignore` and should not be committed.

## Pull Request Notes

- Explain the purpose and scope of the change.
- Mention whether the API, Skill docs, or install flow changed.
- Include local check results.
