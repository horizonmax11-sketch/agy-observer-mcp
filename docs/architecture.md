# 设计与改进范围

```mermaid
flowchart LR
    Client[MCP客户端] --> Server[STDIO / 5个工具]
    Server --> Service[默认关闭执行的Service]
    Service --> Runtime[单次CLI / 双流限额 / 超时清理]
    Runtime --> CLI[用户已安装并登录的agy]
    Runtime --> History[status / events / transcript]
    History --> UI[本地只读页面]
    Service --> Browser[请求打开可见浏览器]
```

## 模块职责

- `config.py`：显式环境配置、执行门禁、超时限额与最小CLI环境。
- `service.py`：一次调用映射一个进程；结果验收；单个活动任务；取消所有权。
- `runtime.py`：并发读stdout/stderr、增量UTF-8解码、严格NDJSON、公开事件过滤、心跳与有限清理。
- `history.py`：只从配置的产物目录读取，验证run_id与链接边界。
- `monitor.py`：同一MCP进程内的loopback只读HTTP线程，随机端口，不需要全局helper/PID管理。
- `server.py`：SDK注册工具与生命周期，stdout只用于MCP协议。
- `static/index.html`：独立历史/对话滚动、真实与模拟分类、公开回复与可折叠事件。

## 相对最初PAL适配的变化

1. 从完整PAL和多提供方依赖中提取，只依赖MCP SDK与标准库。
2. 移除机器专用绝对路径、发行版、用户名、登录HOME、全局配置写入及旧安装脚本。
3. 窗口、记录与运行器共享同一次请求，界面展示不消耗额外模型调用。
4. 原communicate全量等待改为读取时双流限额；超时覆盖管道寿命。
5. 失败/取消都记录终态；同实例拥有的任务才允许取消。
6. 移除与plan冲突的disable-slash-commands组合；仍不夸大权限保证。
7. 监控HTTP是只读组件；MCP执行面与页面执行面没有混用。

## 刻意保留的限制

无自动续聊、文件上传、图像输入、多模型讨论、后台队列、并发付费请求、OAuth代理或公网托管。
该版本的“对话记录”指每次输入与输出的可见记录，不是假装维持同一远端模型会话。
