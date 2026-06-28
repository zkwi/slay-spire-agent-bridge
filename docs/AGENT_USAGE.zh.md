# 在 Codex 或其他 AI Agent 中使用

[English](AGENT_USAGE.en.md) | 中文

本项目的 Agent 使用原则很简单：

```text
bridge 只负责状态和命令队列
AI Agent 只负责决策
所有动作都通过 HTTP API 提交
```

不要让 Agent 直接写 CommunicationMod 的 stdin/stdout。

## Codex Skill 安装

项目内 Skill：

```text
<project>\skills\slay-spire-bridge\SKILL.md
```

Codex 当前安装位置：

```text
%USERPROFILE%\.codex\skills\slay-spire-bridge\SKILL.md
```

如果需要重新安装或同步：

```powershell
Copy-Item -Recurse -Force `
  '.\skills\slay-spire-bridge' `
  "$env:USERPROFILE\.codex\skills\slay-spire-bridge"
```

校验 Skill：

```powershell
$env:PYTHONUTF8='1'
python `
  "$env:USERPROFILE\.codex\skills\.system\skill-creator\scripts\quick_validate.py" `
  "$env:USERPROFILE\.codex\skills\slay-spire-bridge"
```

## Codex 控制循环

读取状态：

```powershell
(Invoke-WebRequest -Uri 'http://127.0.0.1:8787/api/codex_context' -TimeoutSec 5).Content
```

不要这样做：

```powershell
Invoke-RestMethod ... | ConvertTo-Json -Depth 120
```

PowerShell 会在本地对象重新序列化时碰到深度限制，导致 Agent 看不到真正的 JSON。

提交命令：

```powershell
$body = @{
  command = 'choose 1'
  source = 'codex'
  reason = 'Choose the strongest offered card.'
} | ConvertTo-Json -Compress

Invoke-RestMethod `
  -Uri 'http://127.0.0.1:8787/api/command' `
  -Method Post `
  -ContentType 'application/json' `
  -Body $body
```

每次只提交一个命令，然后重新读取状态。

## 其他 AI Agent 使用方式

让 Agent 读取这份 Skill：

```text
<project>\skills\slay-spire-bridge\SKILL.md
```

如果 Agent 不支持 Skill 机制，就把下面这段作为系统/开发者提示词的一部分：

```text
You control Slay the Spire only through http://127.0.0.1:8787.
Read GET /api/codex_context.
Choose exactly one command from suggested_commands.
Submit it with POST /api/command using {"command": "...", "source": "agent", "reason": "..."}.
Re-read context before the next command.
Never write to CommunicationMod stdin/stdout.
Do not use bridge-local AI mode.
```

Python 标准库示例：

```python
import json
import urllib.request

BASE = "http://127.0.0.1:8787"

context = urllib.request.urlopen(
    BASE + "/api/codex_context",
    timeout=5,
).read().decode("utf-8")

print(context)

payload = json.dumps({
    "command": "end",
    "source": "agent",
    "reason": "No useful playable cards remain.",
}).encode("utf-8")

request = urllib.request.Request(
    BASE + "/api/command",
    data=payload,
    headers={"Content-Type": "application/json"},
    method="POST",
)

print(urllib.request.urlopen(request, timeout=5).read().decode("utf-8"))
```

## Agent 决策约束

必须：

- 使用 `/api/codex_context` 或 `/api/commands` 里的 `suggested_commands`。
- 一次只提交一个命令。
- 每次命令后重新读取状态。
- reason 简短说明战术原因。

不要：

- 不要直接根据 raw `available_commands` 拼命令。
- 不要连续提交多个动作。
- 不要在 `paused` 模式下强行提交命令。
- 不要在主菜单自动 `start IRONCLAD 0`，除非用户明确要求新开局。
- 不要向 stdout 打印调试信息。

## 关键屏幕规则

`COMBAT`

- 先斩杀，再防止明显掉血，再打高效输出/抽牌/易伤/成长。

`CARD_REWARD`

- 战斗奖励：只拿优质牌或解决当前牌组问题的牌；普通牌差时跳过。
- 药水或战斗效果产生的选牌：如果 `suggested_commands` 已有 `choose N`，可以直接选。

`HAND_SELECT`

- 看 `screen.selected_cards` 和 `screen.selection`。
- 需要选牌时用 `choose N`。
- 已选好并出现 `proceed` 时确认。

`REST` / `GRID`

- 血量安全优先 smith。
- `screen.purpose=smith_upgrade` 时选强升级，选完通常 `proceed`。

`SHOP_SCREEN`

- 优先移除 Strike/坏牌、强遗物、关键牌；不要买边际收益低的东西。

`MAP`

- 血量和牌组强度允许时拿成长；低血或牌组弱时降低精英风险。

`EXECUTING_ACTIONS`

- 默认等待。
- 例外：`HAND_SELECT` 或 `CARD_REWARD` 这类交互选择屏幕，如果 `suggested_commands` 有 `choose N` 或 `proceed`，可以执行该命令。

`GAME_OVER` / `DEATH`

- 当前 run 已结束，不能继续战斗，也不能通过 bridge 回滚。
- 只使用 `suggested_commands` 里给出的终局命令；如果只有 `wait 30` / `state`，就等待游戏暴露离开死亡界面的命令，或由人类手动回主菜单。
- 回到主菜单后，优先 Continue；没有可继续存档时，只有在用户明确同意时才开始新局。

## 调试顺序

1. `GET /api/codex_context`
2. `GET /api/commands`
3. `GET /api/debug`
4. 查看 `<project>\run\commands.jsonl`
5. 查看 `<project>\run\errors.jsonl`
6. 查看 `<project>\run\agent.log`

常见判断：

- `queued` 后没有 `sent`：bridge 没收到新的游戏状态，或被执行期保护拦住。
- `rejected`：命令不在当前可用命令中。
- 一直 `wait 30`：可能是动画期，也可能是交互选择屏幕未被识别；看 `screen.type` 和 `suggested_commands`。
- Python 修改无效：重启 bridge/game。
