# 配置参考

0.8.0新增直接Claude Code与Codex CLI。渠道、参与者、请求模型及新的环境变量见[多渠道接入](channels.md)。省略新参数时仍使用Antigravity；Claude新请求仅允许Opus，旧Sonnet历史不改写。

| 变量 | 默认值/含义 |
| --- | --- |
| `AGY_OBSERVER_ENABLED` | `0`；只有精确的`1`启用执行 |
| `AGY_OBSERVER_CLI` | `agy`，也可以是CLI绝对路径 |
| `AGY_OBSERVER_MODEL` | `gemini-3.8-flash-low` |
| `AGY_OBSERVER_CLAUDE_MODEL` | `claude-opus-4-6-thinking`；三方协作中的另一独立请求模型 |
| `AGY_OBSERVER_WORKSPACE` | 无默认；执行时必须指向现有目录，建议空目录 |
| `AGY_OBSERVER_STATE_DIR` | Linux/macOS按XDG state路径；Windows按LOCALAPPDATA |
| `AGY_OBSERVER_PORT` | `0`，由操作系统选择空闲loopback端口 |
| `AGY_OBSERVER_OPEN_BROWSER` | `1`；设为`0`是明确选择无界面运行 |
| `AGY_OBSERVER_CLI_TIMEOUT` | 默认 `30` 秒；整数范围 `1..180` |
| `AGY_OBSERVER_OUTER_TIMEOUT` | 默认 `max(40, CLI超时+10)` 秒；不小于 CLI 超时，最高 `210` |
| `AGY_OBSERVER_CLI_HOME` | 可选，指向**已有**CLI登录HOME；不迁移或复制 |
| `AGY_OBSERVER_CLI_XDG_CONFIG_HOME` 等 | 可选，CONFIG/CACHE/DATA/STATE各自指向已有CLI路径 |

CLI子进程只继承常见系统/用户会话环境及上述明确路径，不透传API密钥类环境变量。凭据由官方CLI按照其正常机制使用，桥接服务自身不读凭据内容。

三方模式中 `AGY_OBSERVER_MODEL` 对应 Gemini，`AGY_OBSERVER_CLAUDE_MODEL` 对应 Claude。先用官方 `agy models` 核对当前账号可用名称；这两个名称来自实际列表，并非跨账号保证。模型名称必须不同；真实服务端身份与供应商额度不由本桥接层认证。独立调用使用已有同一 CLI 登录，不复制认证资料。

## Windows与WSL

CLI和本MCP最好运行在同一个操作系统环境。若CLI在WSL中，将Python包安装在该WSL用户的venv中，客户端使用 `wsl.exe -d <发行版> -u <非root用户> --exec <venv-python> -m agy_observer.server`。不要以默认root启动模型。

WSL的窗口打开会尝试系统浏览器，再尝试Windows PowerShell的普通 `Start-Process`；不传ExecutionPolicy或权限绕过参数。浏览器访问WSL loopback依赖WSL的localhost转发。独立版已完成一次 WSL 下真实 CLI 的 MCP 调用；浏览器可从 Windows 访问 WSL 本地页面。此结果仅代表本次环境。如果无图形环境，可明确选择 `AGY_OBSERVER_OPEN_BROWSER=0` 并从 `agy_info` 获取URL。没有额外认证HOME隔离层。


较慢的模型可显式设置 `AGY_OBSERVER_CLI_TIMEOUT=120`、`AGY_OBSERVER_OUTER_TIMEOUT=150`。更长等待可能增加单次费用，不会增加任务次数预算或启用自动重试。超时记录保持原状；调整配置只影响后续显式调用。双流各 2 MiB 限额保持不变。

客户端自己的工具超时也需覆盖外层等待及进程清理，否则客户端会先中断。附带的 Codex 示例设为 `tool_timeout_sec=240`，并列出全部15个工具，包含动态分工的 `agy_council_assign_roles`；其他客户端按其配置方式调整。客户端等待时间不会放宽服务端的180/210秒上限。

## 升级与异常恢复

升级前停止所有使用同一状态目录的 MCP 写入实例，备份私有状态目录，再安装新版本；不要让旧版和新版同时写入同一目录。状态目录应位于本机磁盘，未验证网络文件系统的锁语义。

- **讨论正文缺失，但原始运行成功**：调用 `agy_discussion_recover(discussion_id)`。工具核对运行与任务、消息、模型的关联后，从原始成功回复恢复正文，保留原失败状态、错误和时间的恢复记录；不重跑模型，不退回或增加次数。真实失败的运行不会被改成成功。
- **新版本写入进程崩溃**：操作系统会释放 `writer.guard` 上的内核锁。后续写入取得锁后，可回收本协议的残留 `write.lock`，并追加 `lock-recoveries.jsonl`。`writer.guard` 文件长期保留是正常现象，不要在服务运行时删除它。
- **提示 legacy write.lock**：旧版或无法识别的锁不会自动删除。先停止所有相关写入实例，确认没有仍在执行的模型进程，备份后将状态目录 `discussions/write.lock` 改名保存，再启动新版。无法确认写入者已停止时不要清理。不要删除讨论 JSON 或原始运行记录。
- **并发批次被拒绝**：等待当前批次完成后再显式发起。拒绝发生在次数预留前；已经预留的失败、取消或超时调用仍计入尝试预算。

旧锁本身不能证明讨论文件已经损坏。旧版同样先写临时文件再原子替换，崩溃后可能留下的是上一份完整快照。重启后先用 `agy_discussion_read` 读取任务、`agy_status` 检查关联运行；能读取只证明存储格式可解析，不保证内容最新或语义正确。如果 JSON 损坏，保留原文件与临时文件，在备份副本上检查；优先从已知完整备份恢复。缺少可核对的运行关联时，不猜补编号或强行改成成功；可建立新任务，明确摘录的原始记录来源，保留旧失败记录等待人工核对。

## 常见问题

- 找不到 CLI：确认配置的是官方 `agy` CLI，而不是桌面 IDE；WSL 的 CLI 应搭配 WSL 内的 Python。
- 执行被拒绝：检查开关是否精确为 `1`、工作目录是否存在，以及官方 CLI 自身是否已登录。
- 页面没有记录：真实调用前不会创建对话；离线模拟需勾选“显示离线测试与模拟”。
- 浏览器无法打开：有界面模式会在启动 CLI 前失败；无图形环境可明确设 `AGY_OBSERVER_OPEN_BROWSER=0`。
- 超时或失败：用 `agy_status(run_id)` 查看终态与警告；服务不会自动重试。

CLI 子进程使用参数列表启动，不经过 shell。提示会作为 CLI 参数传递，本机同用户进程可能看到命令行。更多边界见 [SECURITY.md](../SECURITY.md)。
