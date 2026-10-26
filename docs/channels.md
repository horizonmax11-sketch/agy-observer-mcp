# 参与者、渠道与模型

0.8.0 恢复 PAL clink 的 Claude Code、Codex CLI 接口设计，保留 Antigravity。三者使用不同的命令构造器和流式解析器，进入同一个讨论、分工、方案版本、窗口和历史系统。

## 三个配置维度

| 参与者 | 可选渠道 `channel` | 请求模型 `model` | 发言来源 |
| --- | --- | --- | --- |
| `gpt` | `client`，不发起外部请求 | 由当前 MCP 客户端决定 | `source=client`，没有外部 run_id |
| `gemini` | `antigravity` | 如 `gemini-3.8-flash-low` | 官方 agy CLI 的模型回复 |
| `claude` | `antigravity` | 账号可用的 Opus，如 `claude-opus-4-6-thinking` | 经 Antigravity 请求 |
| `claude` | `claude_code` | 官方 CLI 可用的 Opus 别名或完整标识，如 `opus` | 官方 Claude Code CLI |
| `codex` | `codex_cli` | 账号可用的 Codex 模型标识 | 独立 Codex CLI，不是客户端 GPT 的代写发言 |

这些是请求配置，不是对提供方内部身份或额度的认证。模型标识示例不保证所有账号均可用。Claude 新请求只接受 Opus；不会回退 Sonnet，也不会把历史 Sonnet 记录改标成 Opus。

同一个 `claude` 参与者在一场讨论中选择一个渠道。需要比较两个渠道时创建不同任务；不会为同一个角色自动把两边都调用一遍。

## 安装和登录

MCP 与被调用 CLI 应运行在同一个操作系统环境。Windows 安装使用原生 `claude.exe`、`codex.exe`；Linux/WSL 使用该环境内的 `claude`、`codex`。不支持通过 `.cmd`/shell 包装或跨系统 `.exe` 转发冒充同一进程组的取消保证。

1. Antigravity：安装并登录官方 `agy` CLI，用 `agy models` 核对账号可用标识。
2. Claude Code：按[官方安装说明](https://code.claude.com/docs/en/setup)安装，使用 `claude auth login` 登录。`claude auth status` 可供操作者检查，服务不复制登录资料。
3. Codex：按[官方 CLI 说明](https://learn.chatgpt.com/docs/non-interactive-mode)安装并使用 `codex login` 登录；可用 `codex login status` 检查。请求模型应使用该账号支持的标识。

不要把认证输出、登录文件或 API 密钥写进项目配置、讨论材料或发布仓库。未安装的渠道可以暂不配置，使用现有 Antigravity 不受影响。`agy_info` 检查配置和可执行文件，不验证登录或剩余额度。

## 兼容默认值与全局配置

不传新参数时，`agy_run(prompt)` 仍使用 Gemini / Antigravity；默认讨论仍是客户端 GPT、Gemini、Claude，两个外部参与者均走 Antigravity。Claude 默认请求由 Sonnet 调整为 Opus，以满足仅使用 Opus 的要求。显式保留旧 Sonnet 配置时，新请求会报错，历史仍可读取。

新增环境变量：

| 变量 | 默认值与作用 |
| --- | --- |
| `AGY_OBSERVER_CLAUDE_CLI` | `claude`；原生 Claude Code 可执行文件路径 |
| `AGY_OBSERVER_CODEX_CLI` | `codex`；原生 Codex 可执行文件路径 |
| `AGY_OBSERVER_CODEX_MODEL` | `gpt-6.1-sol`；示例请求标识，需核对账号权限 |
| `AGY_OBSERVER_PARTICIPANTS` | 可选 JSON 对象，以参与者名指定默认 `{channel,model}` |
| `AGY_OBSERVER_CLAUDE_CONFIG_DIR` | 可选，引用已有 Claude Code 配置目录；不迁移资料 |
| `AGY_OBSERVER_CODEX_HOME` | 可选，引用已有 Codex HOME；不迁移资料 |

已有 `AGY_OBSERVER_CLI`、`AGY_OBSERVER_MODEL`、`AGY_OBSERVER_CLAUDE_MODEL` 保持 Antigravity 默认配置含义。AGY 的 HOME/XDG 覆盖只应用于 Antigravity，不传给另外两个 CLI。桥接层不转发 API 密钥类环境变量；各官方 CLI 使用自身既有登录机制。

例如让 Claude 默认走 Claude Code，其他参与者不变：

```json
{"claude":{"channel":"claude_code","model":"opus"}}
```

把此 JSON 字符串作为 `AGY_OBSERVER_PARTICIPANTS` 的值。完整 MCP 示例见[多渠道配置](../examples/mcp-multi-cli.json)，保留 Antigravity 默认的例子见[基础配置](../examples/mcp.json)。修改设置后重启对应 MCP 实例。

## 单次调用

工具参数示例：

```json
{"prompt":"检查给定材料中的边界情况，只讨论。","participant":"claude","channel":"claude_code","model":"opus"}
```

```json
{"prompt":"检查给定材料中的兼容性问题，只讨论。","participant":"codex","channel":"codex_cli","model":"gpt-6.1-sol"}
```

这两个例子均传给 `agy_run`。`gpt` 不能作为外部调用参与者；客户端观点用笔记工具记录。切换渠道必须同时明确模型，避免将 AGY 专用标识直接发给 Claude Code。空模型、非法参与者或不支持的渠道组合会拒绝。

双方协作的 `agy_discussion_turn` 同样支持 `participant`、`channel`、`model`；它记录每一轮实际选择。要让一整场任务的渠道和模型固定，使用下方 council 路由快照。

## 多方讨论与分工

创建任务时选 2–4 位参与者，必须包含客户端 `gpt`，以及至少一位外部参与者。以下只启动 Claude Code 与 Codex，不启动 Gemini 或 Antigravity：

```json
{
  "goal":"根据实际项目材料制定改进方案",
  "participants":["gpt","claude","codex"],
  "routes":{
    "claude":{"channel":"claude_code","model":"opus"},
    "codex":{"channel":"codex_cli","model":"gpt-6.1-sol"}
  },
  "max_turns_per_model":3,
  "project":{
    "stage":"理解项目",
    "materials":"填写客户端实际阅读的材料及来源",
    "success_criteria":"填写可验证的完成标准",
    "constraints":"填写调用预算与实际约束",
    "unknowns":"填写尚缺的信息"
  }
}
```

调用 `agy_council_create` 后：

1. 用 `agy_council_note(kind="initial")` 记录客户端 GPT 的理解，诚实标记其上下文。
2. 用 `agy_council_round` 显式调用所选外部参与者，初判封存规则不变。
3. 收齐所选参与者的成功初判后 `agy_council_reveal`，再用 `agy_council_assign_roles` 选角。`assignments` 必须覆盖本任务所有参与者；本例为 gpt/claude/codex。
4. 各 CLI 接收相同机制生成的项目角色提示词、引用、待答问题和草案版本；继续用 council 笔记保存共同方案和修改原因。

每批最多并发两位外部参与者，也可只选一位。省略 round 的 `participants` 时，选择任务模型表中前两位；四人任务应明确选择批次，例如先 gemini/claude，再单独 codex。每批看调用前快照；后续批次才能回应新返回的观点。

渠道和模型在创建任务时保存为 `routes` 快照；不会因为之后修改默认配置而悄悄改变已有任务。更换它们请创建新任务并明确引用原记录。分工和共同草案仍可在同任务内追加版本，预算不重置。

## 各 CLI 的执行与输出

| 渠道 | 参数与输出 | 成功条件 |
| --- | --- | --- |
| Antigravity | 原有 plan/sandbox、stream-json、print-timeout；解析 init/step_update/result | 成功终态、非空公开回复、退出码0 |
| Claude Code | print、verbose、stream-json、partial messages、plan；关闭内置工具并使用空的严格 MCP 配置 | result success、is_error=false、无权限拒绝、退出码0；观察到非Opus模型时拒绝 |
| Codex CLI | exec --json、显式 read-only、on-request 审批、ephemeral；禁用 agy_observer 防止递归调用 | thread/turn 事件、完成的 agent_message、turn.completed、退出码0且没有错误事件 |

Claude Code 的 schema 直接传给 `--json-schema`；Codex 讨论在本次运行目录写入公开 schema 文件，传给 `--output-schema`。动态角色提示词和材料作为本次新请求发送，不复用远端原会话。

Codex 的 JSON 流通常按消息完成事件显示公开文本，不保证逐 token 输出。思考、工具参数及工具输出不写入公开事件或对话正文。未知/损坏事件流、没有终态、非零退出、权限或认证失败均保留为失败，不用部分输出替代成功。

Antigravity 保留 CLI 内部超时与外层捕获上限。两个直接渠道由公共捕获器执行 CLI 超时限制，外层设置继续为清理与客户端等待提供余量。三种渠道共用双流2MiB上限、有界清理和 `agy_cancel`；仅清理本次创建的进程组/进程树。Windows已退出父进程或主动脱离进程组的子进程仍存在原有系统边界。

不会使用 PAL 原先的 acceptEdits、跳过审批、危险全访问默认参数；没有工具桥接替代操作系统隔离的承诺。Codex 仍会读取其正常用户/项目配置和允许的本机上下文，其他 MCP、hooks 或规则的行为属于该 CLI 配置，应由操作者审查。

## 历史与故障

新运行与模型消息保存 `participant`、`channel`、`model_requested`、`source`、`run_id`、状态；CLI 有公开报告时另存 `model_reported`。客户端笔记固定标为 client/gpt，没有外部 run_id。窗口、历史摘要和可读记录展示渠道来源，Codex CLI 与客户端 GPT 分开标识。

旧记录缺失渠道时显示“未记录（旧版本）”，不改写真实历史。旧默认 council、角色提示词和方案版本继续可读。恢复新记录必须核对任务、消息、参与者、渠道与模型关联，不重发请求。

缺少 CLI：在该 MCP 运行环境安装对应官方 CLI 或改用你明确选择且已配置的渠道。缺少登录、Opus权限或所选模型：由操作者修复该 CLI 的认证/账号配置，服务不换渠道、不换模型、不自动重试。CLI 存在不等于已登录；离线测试与 MCP 握手不证明真实模型请求成功。
