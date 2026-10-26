# 安全与隐私边界

- 模型执行默认关闭，启用与费用授权由操作方和MCP客户端决定。
- 无shell命令拼接、额外CLI参数、权限绕过、自动重试、自动登录、认证文件读取或迁移。
- CLI使用用户已有认证会话；显式设置CLI HOME/XDG路径时，只作为环境路径传给CLI。
- prompt、公开回复和stderr会明文写入配置的state目录。不要在未授权提示中放入密钥；不要发布该目录。
- HTTP页面只允许loopback Host，固定静态页面与只读历史API，拒绝任意文件路径、目录遍历和产物symlink。
- loopback不等于用户级认证：同机其他进程可能读取此页。不要转发、反向代理或暴露到公网。
- plan/sandbox未被本项目证明为强制只读；CLI可能读取/修改其运行环境中的资料。工作目录限制不是文件系统沙箱。
- 每流读取限额2MiB；输出超限、非法流、非成功结果或权限软拒绝均失败关闭。
- Linux以新进程组TERM/KILL清理；主动脱离进程组的daemon不在保证范围内。
- Windows以本次PID树清理；父进程已退出后不保证后代清理，不声称Job Object级控制。
- 页面仅显示公开回复片段、步骤类型与工具名；不记录tool参数或内部思考文本，但这不证明CLI没有其他内部行为。

发现问题时优先通过私有渠道联系仓库所有者。请勿在公开Issue粘贴令牌、真实会话日志或认证文件。

历史状态读取上限5 MiB，事件读取末尾256 KiB内最近100条完整记录；磁盘历史不自动过期，应由操作方管理保留期。

协作任务也保存明文材料，需与runs一样保护。客户端笔记来源未经身份认证，职责提示不是工具权限隔离；上下文由调用端明确提交并会传给配置的模型。模型尝试预算是任务级；三方模式允许实例内两个不同配置模型并发，普通调用仍不重叠，不构成多实例全局额度控制。

三方首轮封存是流程控制，不是保密边界；本机操作者可通过运行状态或磁盘读取原文。`initial_context=fresh` 对客户端发言只是声明，不能证明来源或隔离程度。独立 CLI 进程不续聊，不能证明模型提供商没有内部共享状态。结构化依据、引用及模型名称均需核查，不构成身份认证、事实验证或共同认可。


## 0.8.0 direct CLI channels

Claude Code runs in plan mode with built-in tools disabled and an empty strict MCP configuration. Codex runs with an explicit read-only sandbox and on-request approval; this bridge is disabled in the nested Codex configuration to avoid recursion. PAL's old permission-bypass defaults are not carried over. The native CLIs can still load normal user/project configuration, hooks or instructions; review those in your own installation. A prompt is not an OS sandbox.

Each CLI has a separate stream parser; reasoning/tool payloads are excluded from public events. A successful terminal event and exit code zero are both required. Channel, participant, requested model and run identity are saved separately; client GPT notes never acquire an external run ID. Existing files without channel provenance remain readable and are not relabeled. Cross-OS executable forwarding and shell/.cmd wrappers are unsupported; install the CLI in the MCP operating system so bounded process-group cleanup applies. Existing limitations for detached children remain.

No credential migration, authentication-file inspection, channel/model fallback or automatic retry is performed. Official CLIs use their existing login locations. `agy_info` checks executable availability, not authentication or account model entitlement.
