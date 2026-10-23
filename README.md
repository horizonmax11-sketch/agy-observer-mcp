# AGY Observer MCP

让 MCP 客户端调用 Antigravity CLI，并在本地窗口查看问题、公开回复和运行状态。

**一次调用、一份记录、一个可见窗口。** 仅依赖 MCP Python SDK 与标准库，无需完整 PAL 或前端构建工具。当前版本 **0.2.0 Alpha**。

![界面预览：图中内容为离线模拟](docs/demo.png)

[快速开始](#快速开始) · [连接 MCP](#连接-mcp) · [使用方式](#使用方式) · [配置参考](docs/configuration.md) · [设计说明](docs/architecture.md) · [验证范围](docs/validation.md)

## 主要功能

- **看得到对话**：调用前请求打开本地窗口，流式显示公开回复、警告与真实终态。
- **历史清晰**：左侧历史与右侧对话独立滚动；离线测试、模拟记录默认隐藏。
- **执行可控**：默认关闭模型执行；单个活动调用，不自动重试或切换模型。
- **记录可追踪**：每次保存状态、公开事件和 Markdown 对话记录。
- **读取更轻**：列表只传摘要；完整对话按选中项加载，事件展开后才读取。

页面只读，不发送模型请求。“历史”是独立调用的集合；当前不支持沿同一远端会话自动续聊、文件上传或后台队列。

## 快速开始

需要 **Python 3.10+**。真实调用还需要你另行安装并登录官方 `agy` CLI；桌面 IDE 不能代替 CLI。先下载仓库源码，在项目根目录执行：

**Windows PowerShell**

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install .
.\.venv\Scripts\python.exe -m agy_observer.demo --state-dir ./state --port 8766 --open
```

**Linux / macOS**

```sh
python3 -m venv .venv
.venv/bin/python -m pip install .
.venv/bin/python -m agy_observer.demo --state-dir ./state --port 8766 --open
```

演示仅写入合成内容，**不启动真实 CLI、不调用模型**。窗口会明确标注“离线模拟”；按 `Ctrl+C` 退出。开发验证可使用同一个虚拟环境运行 `python -m unittest discover -s tests -v`。

## 连接 MCP

将客户端指向刚创建的虚拟环境 Python，并替换以下绝对路径。Windows 的 Python 路径为 `.venv\Scripts\python.exe`；JSON 内反斜杠需转义，或使用正斜杠。

```json
{
  "mcpServers": {
    "agy-observer": {
      "command": "/absolute/path/to/.venv/bin/python",
      "args": ["-m", "agy_observer.server"],
      "env": {
        "AGY_OBSERVER_ENABLED": "0",
        "AGY_OBSERVER_CLI": "/absolute/path/to/agy",
        "AGY_OBSERVER_WORKSPACE": "/absolute/path/to/empty-workspace",
        "AGY_OBSERVER_STATE_DIR": "/absolute/path/to/private-state"
      }
    }
  }
}
```

配置后重启客户端，先调用 `agy_info` 确认工具可用。确认费用、工作目录与执行授权后，将 `AGY_OBSERVER_ENABLED` 改为 `1` 并重新启动 MCP。模型请求可能产生费用及 CLI 工具副作用；客户端仍需自行管理审批。

默认请求模型为 `gemini-3.8-flash-low`，是否可用取决于你的账号和 CLI。完整变量、WSL 配置及排错见 [配置参考](docs/configuration.md)。另附 [通用 JSON](examples/mcp.json) 与 [Codex TOML](examples/codex-config.toml)。

## 使用方式

| 工具 | 用途 | 启动模型 |
| --- | --- | --- |
| `agy_info()` | 查看版本、执行开关、模型和限额 | 否 |
| `agy_run(prompt)` | 发起一次已授权请求，返回回复与 `run_id` | 开关开启时，是 |
| `agy_history(limit=20, include_tests=false)` | 列出历史摘要及问题预览 | 否 |
| `agy_status(run_id)` | 获取该次完整问题、回复、警告和终态 | 否 |
| `agy_cancel(run_id)` | 请求取消本实例拥有的活动调用 | 否 |

例如向 MCP 客户端发出：“请用 agy_run 解释这个函数的边界情况，只讨论，不修改文件。”调用前会请求打开窗口；结束后用返回的 `run_id` 查询详情。提示词中的“只讨论”不是强制权限隔离。

每次记录位于私有状态目录：

```text
runs/<run_id>/
  status.json     # 当前状态与完整公开问答
  events.jsonl    # 追加的公开事件
  transcript.md   # 结束时生成的可读记录
```

页面中的“离线测试”（旧记录可能叫“夹具”）指测试用的预设数据或模拟进程，**不是实际模型对话**。勾选后才能查看。页面心跳只表示仍在等待，不代表模型完成百分比。

### 从 0.1 升级

`agy_history` 在 0.2 中改为返回摘要，不再包含完整 `task`、`response`、`warnings`；使用 `task_preview` 预览问题，通过 `agy_status(run_id)` 获取完整内容。已有状态文件可继续读取。

## 验证与边界

离线测试覆盖 MCP 握手、执行门禁、严格事件解析、双流限额、超时/取消、历史读取和本地只读路由。独立包还完成过 **一次真实的 MCP → WSL CLI → 模型 → MCP 回复**审阅调用；该次在本轮改进前执行，修改后的回归验证使用离线测试。具体平台与结果见 [验证记录](docs/validation.md)。

CLI 限时 30 秒，外层捕获限时 40 秒，清理另有有限等待；stdout/stderr 各限制 2 MiB。`--mode plan --sandbox` 不构成已验证的强制只读保证；进程组/进程树清理也有平台边界，详见 [安全与隐私](SECURITY.md)。

历史会明文保存提示和公开回复。**不要上传状态目录、登录 HOME、CLI 二进制、虚拟环境或真实对话。** 本地页面仅绑定 `127.0.0.1`，不适合公网部署。

## 来源与许可证

项目由对 PAL MCP Server 的本地适配工作整理而来，保留 [来源与改动说明](NOTICE)，采用 [Apache-2.0](LICENSE)。不是 Google、PAL 或 OpenAI 的官方产品。

参考：[Antigravity headless 事件格式](https://antigravity.google/docs/cli/headless/) · [MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk)。
