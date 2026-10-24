# 配置参考

| 变量 | 默认值/含义 |
| --- | --- |
| `AGY_OBSERVER_ENABLED` | `0`；只有精确的`1`启用执行 |
| `AGY_OBSERVER_CLI` | `agy`，也可以是CLI绝对路径 |
| `AGY_OBSERVER_MODEL` | `gemini-3.8-flash-low` |
| `AGY_OBSERVER_CLAUDE_MODEL` | `claude-sonnet-4-6`；三方协作中的另一独立请求模型 |
| `AGY_OBSERVER_WORKSPACE` | 无默认；执行时必须指向现有目录，建议空目录 |
| `AGY_OBSERVER_STATE_DIR` | Linux/macOS按XDG state路径；Windows按LOCALAPPDATA |
| `AGY_OBSERVER_PORT` | `0`，由操作系统选择空闲loopback端口 |
| `AGY_OBSERVER_OPEN_BROWSER` | `1`；设为`0`是明确选择无界面运行 |
| `AGY_OBSERVER_CLI_HOME` | 可选，指向**已有**CLI登录HOME；不迁移或复制 |
| `AGY_OBSERVER_CLI_XDG_CONFIG_HOME` 等 | 可选，CONFIG/CACHE/DATA/STATE各自指向已有CLI路径 |

CLI子进程只继承常见系统/用户会话环境及上述明确路径，不透传API密钥类环境变量。凭据由官方CLI按照其正常机制使用，桥接服务自身不读凭据内容。

三方模式中 `AGY_OBSERVER_MODEL` 对应 Gemini，`AGY_OBSERVER_CLAUDE_MODEL` 对应 Claude。先用官方 `agy models` 核对当前账号可用名称；这两个名称来自实际列表，并非跨账号保证。模型名称必须不同；真实服务端身份与供应商额度不由本桥接层认证。独立调用使用已有同一 CLI 登录，不复制认证资料。

## Windows与WSL

CLI和本MCP最好运行在同一个操作系统环境。若CLI在WSL中，将Python包安装在该WSL用户的venv中，客户端使用 `wsl.exe -d <发行版> -u <非root用户> --exec <venv-python> -m agy_observer.server`。不要以默认root启动模型。

WSL的窗口打开会尝试系统浏览器，再尝试Windows PowerShell的普通 `Start-Process`；不传ExecutionPolicy或权限绕过参数。浏览器访问WSL loopback依赖WSL的localhost转发。独立版已完成一次 WSL 下真实 CLI 的 MCP 调用；浏览器可从 Windows 访问 WSL 本地页面。此结果仅代表本次环境。如果无图形环境，可明确选择 `AGY_OBSERVER_OPEN_BROWSER=0` 并从 `agy_info` 获取URL。没有额外认证HOME隔离层。


超时与双流限额是代码内的固定上限，未提供放宽上限的环境变量。

## 常见问题

- 找不到 CLI：确认配置的是官方 `agy` CLI，而不是桌面 IDE；WSL 的 CLI 应搭配 WSL 内的 Python。
- 执行被拒绝：检查开关是否精确为 `1`、工作目录是否存在，以及官方 CLI 自身是否已登录。
- 页面没有记录：真实调用前不会创建对话；离线模拟需勾选“显示离线测试与模拟”。
- 浏览器无法打开：有界面模式会在启动 CLI 前失败；无图形环境可明确设 `AGY_OBSERVER_OPEN_BROWSER=0`。
- 超时或失败：用 `agy_status(run_id)` 查看终态与警告；服务不会自动重试。

CLI 子进程使用参数列表启动，不经过 shell。提示会作为 CLI 参数传递，本机同用户进程可能看到命令行。更多边界见 [SECURITY.md](../SECURITY.md)。
