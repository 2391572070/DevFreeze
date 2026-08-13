# DevFreeze

[![CI](https://github.com/2391572070/DevFreeze/actions/workflows/ci.yml/badge.svg)](https://github.com/2391572070/DevFreeze/actions/workflows/ci.yml)

**保存本地开发任务的上下文，稍后通过一份由你审核的计划恢复工作。**

DevFreeze 是一个本地优先、运行时零第三方依赖的命令行工具。它保存的是
开发现场的元数据：工作区位置、当时看到的 Git 状态、相关工具版本、可选的
交接笔记，以及通过项目配置声明或由 DevFreeze 托管的服务。

DevFreeze **不会**保存环境变量值，也不会复制未提交文件的内容。恢复过程不会
切换分支、重置 Git、安装依赖或覆盖源代码。

[English README](../README.md) · [开发路线](ROADMAP.md) ·
[架构说明](architecture.md) · [安全策略](../SECURITY.md) ·
[贡献指南](../CONTRIBUTING.md)

> DevFreeze 目前是 alpha 软件。在敏感项目中使用前，请检查快照 JSON 和恢复计划。

## 为什么需要它

隔几天重新处理一项任务时，常常需要重新拼凑许多小信息：当时在哪个目录和
分支、哪些文件有修改、使用了什么运行时、哪些本地服务与任务有关，以及下一步
本来准备做什么。DevFreeze 把这些元数据保存在一起，但不复制工作区内容。

## 安装

要求 Python 3.11 或更高版本；若希望捕获仓库状态，还应安装 Git。

```console
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e .
devfreeze doctor
```

DevFreeze 没有第三方运行时依赖，构建过程使用 setuptools。

## 快速开始

保存当前现场：

```console
devfreeze freeze payment-bug -m "已复现超时；下一步检查 retry.py"
devfreeze list
devfreeze show payment-bug
```

对比当前状态并预览恢复计划：

```console
devfreeze diff payment-bug
devfreeze thaw payment-bug
```

`thaw` 默认只预览。审核计划后，显式允许 DevFreeze 启动快照中缺失的服务：

```console
devfreeze thaw payment-bug --execute
```

如果分支、HEAD 或未提交文件列表发生漂移，还需明确认可当前状态：

```console
devfreeze thaw payment-bug --execute --force
```

这里的 `--force` 只是对漂移的确认，不会让 DevFreeze 修改 Git 或文件。交互执行
仍会要求确认；`-y` 可以跳过这个确认提示。

## 托管本地服务

以前台方式运行并记录一个服务的精确参数：

```console
devfreeze run --name web --port 3000 --ready-url http://localhost:3000/health -- npm run dev
```

也可以放到后台运行：

```console
devfreeze run --name worker --detach -- python -m app.worker
devfreeze services
devfreeze stop worker
```

分隔符 `--` 用于明确区分 DevFreeze 参数和被托管命令。命令以参数数组保存和
执行（`shell=False`），因此 `cmd1 && cmd2` 之类的 Shell 表达式不会被解释。

服务名按最近的项目根目录隔离，因此不同项目可以同时运行名为 `web` 的
服务。请在对应项目内执行 `devfreeze stop NAME`；若配置已移动或删除，
可执行 `devfreeze stop NAME --workspace /原始/根目录`。托管日志与注册表文件
使用仅当前用户可读写的权限。

`--port` 可以重复使用。Linux 上，DevFreeze 可以通过 `/proc` 检测托管进程及
其子进程监听的端口。macOS 和 Windows 会安全降级：声明的端口仍然保留，但
自动检测可能得不到任何结果。

Windows 上请直接调用可执行文件或脚本运行时（例如
`node path\\to\\script.js`）。DevFreeze 会拒绝 `.cmd`/`.bat` 转接脚本，因为它们
必须通过 `cmd.exe` 字符串求值，会破坏精确参数的安全边界。

## 项目配置

在项目根目录提交 `.devfreeze.toml`，即可声明应写入快照的服务：

```toml
version = 1

[[services]]
name = "web"
command = ["python", "-m", "app"]
cwd = "."
ports = [8000]
ready_url = "http://localhost:8000/health"

[[services]]
name = "worker"
command = ["python", "-m", "app.worker"]
```

`command` 必须是字符串数组。服务的 `cwd` 是项目根目录内的相对路径，不能逃逸
到项目外。配置采用严格校验：未知字段和非法值会直接报错，不会被猜测或忽略。

## 命令速查

```text
devfreeze freeze NAME [-m NOTE] [--force] [--json]
devfreeze list [--json]
devfreeze show NAME [--json]
devfreeze diff NAME [--json]
devfreeze thaw NAME [--execute] [-y] [--force] [--json]
devfreeze run --name NAME [--cwd PATH] [--port N] [--ready-url URL]
              [--detach] -- COMMAND...
devfreeze services [--all] [--json]
devfreeze stop NAME [--workspace PATH] [--force]
devfreeze delete NAME [-y]
devfreeze doctor
devfreeze version
devfreeze --version
```

运行 `devfreeze COMMAND --help` 可查看具体选项。全局选项 `--data-dir PATH`
必须放在子命令之前。

## 数据位置与隐私

快照 JSON 默认存放在以下第一个可用位置：

1. `$DEVFREEZE_HOME`；
2. `$XDG_DATA_HOME/devfreeze`；
3. `~/.local/share/devfreeze`。

单次调用可用 `--data-dir PATH` 覆盖。快照文件名为 `<name>.json`；运行期服务
状态存放在 `runtime/` 下，避免与名为 `services` 的快照冲突。

快照是便于人工审阅的元数据，但其中仍可能出现路径、仓库远程地址、分支名、
变更文件名、命令、URL 和笔记。分享前请检查 JSON。完整磁盘格式见
[快照 Schema](snapshot.schema.json)，威胁模型见 [SECURITY.md](../SECURITY.md)。

## 安全边界

- 不采集环境变量值；
- 不采集未提交文件内容或补丁；
- 命令以参数数组执行，不通过 Shell 求值；
- `thaw` 默认只预览，只有 `--execute` 才会启动服务；
- 只启动快照中记录且当前缺失的服务；
- Git 漂移需要 `--force`，但该选项仅表示认可当前状态；
- 恢复期间绝不执行 `checkout`、`reset`、依赖安装或源文件替换。

## 开发

```console
make test
make check
```

测试使用 Python 标准库 `unittest`。参与项目前请阅读[开发路线](ROADMAP.md)和
[贡献指南](../CONTRIBUTING.md)。公开 Bug 与功能建议请使用
[Issue 选择器](https://github.com/2391572070/DevFreeze/issues/new/choose)；疑似安全
漏洞必须按照[安全策略](../SECURITY.md)私下报告，不要创建公开 Issue。

## 许可证

DevFreeze 使用 [MIT License](../LICENSE)。
