# 安装 Mod 与配置 CommunicationMod

[English](INSTALL_MODS.en.md) | 中文

这部分只讲让本项目跑起来需要的游戏侧环境。

## 需要哪些前置内容

必需：

```text
Slay the Spire
ModTheSpire
BaseMod
Communication Mod
Python 环境
agent_bridge.py
```

Communication Mod 的 Steam Workshop 页面列出的 required items 是 `ModTheSpire` 和 `BaseMod`。CommunicationMod GitHub README 也列出 Requirements：Slay the Spire、ModTheSpire、BaseMod。

BaseMod 的 GitHub README 还说明普通使用需要 Java 8 和 ModTheSpire。Steam 版通常会通过 Slay the Spire / ModTheSpire 环境处理 Java；手动安装时才需要更关心 Java 版本。

可选但建议：

```text
游戏设置中开启 Fast Mode
```

CommunicationMod README 提到它没有在关闭 fast mode 的情况下测试过。这里说的是游戏运行模式建议，不是必须再装一个额外 mod。

## 推荐安装方式：Steam Workshop

在 Steam Workshop 订阅：

1. [ModTheSpire](https://steamcommunity.com/workshop/filedetails/?id=1605060445)
2. [BaseMod](https://github.com/daviscook477/BaseMod) 或 Steam Workshop 里的 BaseMod 条目
3. [Communication Mod](https://steamcommunity.com/workshop/filedetails/?id=2131373661)

然后从 Steam 启动 Slay the Spire 时选择：

```text
Play With Mods
```

在 ModTheSpire 窗口勾选：

```text
BaseMod
Communication Mod
```

Communication Mod 自身不会玩游戏，它只负责启动外部进程并通过 stdin/stdout 交换 JSON 状态和命令。

## 手动安装方式

不推荐优先使用手动安装，除非 Steam Workshop 不可用。

大致流程：

1. 安装或准备 ModTheSpire。
2. 把 `BaseMod.jar` 放到 ModTheSpire 识别的 mods 目录。
3. 把 `CommunicationMod.jar` 放到同一个 mods 目录。
4. 运行 ModTheSpire，勾选 BaseMod 和 CommunicationMod。

mods 目录位置取决于你的 ModTheSpire 安装方式。常见位置包括：

```text
<SlayTheSpire>\mods
%LOCALAPPDATA%\ModTheSpire\mods
```

如果不确定，以 ModTheSpire 启动窗口和日志显示的位置为准。

## 配置 CommunicationMod 启动本项目

CommunicationMod 首次运行后会创建配置目录。配置文件通常位于：

```text
%LOCALAPPDATA%\ModTheSpire\CommunicationMod\config.properties
```

本项目推荐配置：

```properties
verbose=true
maxInitializationTimeout=30
command=<python.exe> <project>\\agent_bridge.py
runAtGameStart=true
```

Windows 示例：

```properties
command=C\:\\Path\\To\\Python\\python.exe C\:\\Path\\To\\slay-spire-agent\\agent_bridge.py
```

注意：

- `command` 必须指向 Python 解释器和 `agent_bridge.py`。
- Java properties 里 Windows 路径的 `\` 通常需要写成 `\\`，盘符冒号可写成 `C\:`。
- 路径里有空格时更容易出错，尽量使用无空格路径。
- 修改 Python 文件后，已经运行的 bridge 不会热加载；需要重启游戏/ModTheSpire/bridge。

## 协议注意事项

CommunicationMod 会启动外部进程，并等待外部进程向 stdout 输出：

```text
ready
```

之后它会把稳定后的游戏状态 JSON 发到外部进程 stdin，并等待外部进程从 stdout 返回命令。

因此本项目有一个硬规则：

```text
stdout 只能输出给 CommunicationMod 的协议命令
调试信息必须写日志文件，不能 print 到 stdout
```

常见命令包括：

```text
play 1 0
choose 1
proceed
return
end
wait 30
state
```

## 验证是否启动成功

启动 Modded Slay the Spire 后检查：

```text
<project>\run\server.txt
<project>\run\agent.log
<project>\run\latest_summary.txt
```

浏览器打开：

```text
http://127.0.0.1:8787
```

或者 PowerShell：

```powershell
(Invoke-WebRequest -Uri 'http://127.0.0.1:8787/api/summary' -TimeoutSec 5).Content
```

## 常见问题

`No state received yet`

- bridge 的 HTTP 服务启动了，但 CommunicationMod 还没有发状态。
- 确认游戏已通过 ModTheSpire 启动，并勾选 Communication Mod。

`communication_mod_errors.log` 有内容

- 优先看里面的 Python 路径、脚本路径、ready 超时、异常栈。
- CommunicationMod README 也建议通过这个文件调试外部进程启动问题。

命令入队但游戏不动

- 看 `run/commands.jsonl`，确认是否从 `queued` 变成 `sent`。
- 看 `/api/debug` 的 queue size 和最新 `action_phase`。
- 如果刚修改了 Python，先重启 bridge。

主菜单不能自动 Continue

- CommunicationMod 不一定暴露 Continue 协议命令。
- 如果本地有 autosave 但没有 Continue choice，bridge 会阻止自动 `start`，需要人类先在游戏里点 Continue。
