# Security Policy

English | [中文](SECURITY.zh.md)

## Supported Versions

This project is in an early stage. Security fixes primarily target the main branch.

## Reporting a Vulnerability

If you find any of the following issues, please contact the maintainer privately. Do not publicly post real secrets or reproducible leaked data.

- API keys, tokens, cookies, or other credentials are exposed.
- Local usernames, absolute paths, save paths, or runtime logs are accidentally committed.
- The HTTP API is exposed to a non-local network and allows unauthorized game control.
- Logs or error messages are not properly redacted.

## Local Security Defaults

- The HTTP server listens on `127.0.0.1` by default.
- Runtime logs are written to `run/`, which should not be committed.
- Model config is stored in `llm_config.local.json`, which should not be committed.
- External LLM calls are disabled by default.

## Pre-Release Check

Before publishing, run:

```powershell
rg -n "sk-[A-Za-z0-9_-]{20,}|Bearer [A-Za-z0-9_.-]{20,}|\\.env" .
python -m py_compile agent_bridge.py
```
