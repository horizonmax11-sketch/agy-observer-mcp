"""PAL clink command/parser separation, adapted to bounded public-event capture.

Derived from PAL's clink agents/claude.py, agents/codex.py and parsers. See NOTICE.
Unlike the original recovery parsers, partial/error output cannot become success.
"""

from dataclasses import dataclass
import json
import re

CHANNELS = {
    "gemini": {"antigravity"},
    "claude": {"antigravity", "claude_code"},
    "codex": {"codex_cli"},
}
LABELS = {"gpt": "GPT（客户端）", "gemini": "Gemini", "claude": "Claude", "codex": "Codex CLI"}


def model_label(value):
    if (
        not isinstance(value, str)
        or not value
        or value.startswith("-")
        or len(value) > 160
        or any(ord(c) < 32 for c in value)
    ):
        raise ValueError("Configure a nonempty model identifier (max 160 characters)")
    return value


@dataclass(frozen=True)
class Endpoint:
    participant: str
    channel: str
    model: str

    def __post_init__(self):
        if self.channel not in CHANNELS.get(self.participant, set()):
            raise ValueError(
                "Unsupported participant/channel combination; GPT client notes are not external calls"
            )
        model_label(self.model)
        if self.participant == "claude" and not re.search(
            r"(?:^|[-_.])opus(?:$|[-_.])", self.model.lower()
        ):
            raise ValueError(
                "Claude requests require an available Opus identifier; no Sonnet fallback"
            )

    def public(self):
        return dict(channel=self.channel, model=self.model)

    def command(self, executable, prompt, timeout, schema=None):
        if self.channel == "antigravity":
            result = [
                executable,
                "--output-format",
                "stream-json",
                "--mode",
                "plan",
                "--sandbox",
                "--print-timeout",
                f"{timeout}s",
                "--model",
                self.model,
                "-p",
                prompt,
            ]
            if schema:
                result += ["--json-schema", json.dumps(schema, separators=(",", ":"))]
            return result
        if self.channel == "claude_code":
            result = [
                executable,
                "--print",
                "--verbose",
                "--output-format",
                "stream-json",
                "--include-partial-messages",
                "--permission-mode",
                "plan",
                "--tools",
                "",
                "--strict-mcp-config",
                "--mcp-config",
                '{"mcpServers":{}}',
                "--model",
                self.model,
            ]
            if schema:
                result += ["--json-schema", json.dumps(schema, separators=(",", ":"))]
            return result + ["--", prompt]
        # Explicit read-only sandbox. Keep approvals; never --full-auto / --yolo.
        # Disable this bridge to prevent recursive Codex -> MCP -> Codex calls.
        result = [
            executable,
            "--ask-for-approval",
            "on-request",
            "exec",
            "--json",
            "--sandbox",
            "read-only",
            "--skip-git-repo-check",
            "--ephemeral",
            "-c",
            "mcp_servers.agy_observer.enabled=false",
            "--model",
            self.model,
        ]
        if schema:
            result += ["--output-schema", str(schema)]
        return result + ["--", prompt]


class Stream:
    def __init__(self):
        self.started = False
        self.terminal = False
        self.payload = None
        self.text = ""
        self.reported_model = None
        self.conversation_id = None

    def finish(self):
        if not self.terminal or not isinstance(self.payload, dict):
            raise ValueError("CLI stream has no complete terminal result")
        return self.payload

    def guard(self, event):
        if not isinstance(event, dict):
            raise ValueError("Stream event must be an object")
        if self.terminal:
            raise ValueError("Event after terminal result")


class AntigravityStream(Stream):
    def feed(self, event):
        self.guard(event)
        label = event.get("event")
        if label not in {"init", "step_update", "result"}:
            raise ValueError("Unrecognized Antigravity event")
        if label == "init":
            if self.started or not isinstance(event.get("init", {}), dict):
                raise ValueError("Invalid or duplicate init")
            self.started = True
            self.conversation_id = event.get("conversation_id")
            self.reported_model = event.get("init", {}).get("model")
            return "init", {
                "model": self.reported_model,
                "permission_mode": event.get("init", {}).get("permission_mode"),
            }
        if not self.started:
            raise ValueError("Stream event before init")
        if label == "result":
            self.terminal, self.payload = True, event.get("result")
            return "result", {}
        step = event.get("step_update")
        if not isinstance(step, dict):
            raise ValueError("Invalid stream step")
        public = {
            k: step[k] for k in ("step_index", "state", "step_type", "tool_name") if k in step
        }
        if step.get("step_type") == "agent_response":
            delta = step.get("text_delta", "")
            if not isinstance(delta, str):
                raise ValueError("Invalid response delta")
            self.text += delta
            public["text_delta"] = delta
        return "step_update", public


class ClaudeStream(Stream):
    def __init__(self):
        super().__init__()
        self.messages = {}

    def feed(self, event):
        self.guard(event)
        kind = event.get("type")
        if kind == "system":
            if event.get("subtype") == "init":
                if self.started:
                    raise ValueError("Duplicate Claude init")
                self.started = True
                self.conversation_id = event.get("session_id")
                self.reported_model = event.get("model")
                return "init", {
                    "model": self.reported_model,
                    "permission_mode": event.get("permissionMode"),
                }
            return "metadata", {}
        if not self.started:
            raise ValueError("Claude event before system init")
        if kind == "stream_event":
            delta = event.get("event", {}).get("delta", {})
            if delta.get("type") == "text_delta" and isinstance(delta.get("text"), str):
                self.text += delta["text"]
                return "step_update", {"text_delta": delta["text"]}
            return "metadata", {}  # Never store thinking/signature/tool input deltas.
        if kind == "assistant":
            message = event.get("message", {})
            self.reported_model = message.get("model") or self.reported_model
            if self.reported_model and "opus" not in self.reported_model.lower():
                raise ValueError("Claude reported a non-Opus model; result rejected")
            blocks = message.get("content", [])
            if not isinstance(blocks, list):
                raise ValueError("Invalid Claude assistant content")
            text = "".join(
                b["text"]
                for b in blocks
                if isinstance(b, dict)
                and b.get("type") == "text"
                and isinstance(b.get("text"), str)
            )
            self.messages[message.get("id") or event.get("uuid") or str(len(self.messages))] = text
            # Snapshot events duplicate partial deltas; final result replaces the preview.
            if not self.text:
                self.text = text
            return "step_update", {}
        if kind == "result":
            self.terminal = True
            response = event.get("result")
            if isinstance(event.get("structured_output"), dict):
                response = json.dumps(event["structured_output"], ensure_ascii=False)
            if not isinstance(response, str) or not response.strip():
                response = "\n\n".join(self.messages.values())
            success = event.get("subtype") == "success" and event.get("is_error") is False
            if event.get("permission_denials"):
                success = False
            self.payload = dict(
                status="SUCCESS" if success else "ERROR",
                response=response,
                conversation_id=event.get("session_id") or self.conversation_id,
                usage=event.get("usage"),
            )
            return "result", {}
        if kind in {"user", "rate_limit_event", "tool_progress", "tool_use_summary"}:
            return "metadata", {}
        raise ValueError("Unrecognized Claude stream event")


class CodexStream(Stream):
    def __init__(self):
        super().__init__()
        self.messages = {}
        self.turn_started = False
        self.error = False

    def feed(self, event):
        self.guard(event)
        kind = event.get("type")
        if kind == "thread.started":
            if self.started:
                raise ValueError("Duplicate Codex thread")
            self.started = True
            self.conversation_id = event.get("thread_id")
            self.reported_model = event.get("model")
            return "init", {"model": self.reported_model}
        if kind == "error":
            self.error = True
            return "error", {}  # Raw error payloads may contain private arguments.
        if not self.started:
            raise ValueError("Codex event before thread.started")
        if kind == "turn.started":
            if self.turn_started:
                raise ValueError("Duplicate Codex turn")
            self.turn_started = True
            return "metadata", {}
        if kind in {"turn.completed", "turn.failed"}:
            self.terminal = True
            self.payload = dict(
                status=(
                    "SUCCESS"
                    if kind == "turn.completed" and not self.error and self.turn_started
                    else "ERROR"
                ),
                response="\n\n".join(self.messages.values()),
                conversation_id=self.conversation_id,
                usage=event.get("usage"),
            )
            return "result", {}
        if kind in {"item.started", "item.updated", "item.completed"}:
            item = event.get("item")
            if not isinstance(item, dict):
                raise ValueError("Invalid Codex item")
            if item.get("type") == "agent_message" and isinstance(item.get("text"), str):
                if kind == "item.completed":
                    self.messages[item.get("id") or str(len(self.messages))] = item["text"]
                    self.text = "\n\n".join(self.messages.values())
                return "step_update", {}
            return "metadata", {}  # Never persist reasoning, commands, tool args or outputs.
        raise ValueError("Unrecognized Codex stream event")


def stream_parser(channel):
    return {
        "antigravity": AntigravityStream,
        "claude_code": ClaudeStream,
        "codex_cli": CodexStream,
    }[channel]()
