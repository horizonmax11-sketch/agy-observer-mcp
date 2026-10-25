"""One MCP call maps to one CLI process; no retries, login automation, or shell arguments."""

import asyncio
import math
import json
import shutil
from pathlib import Path

from .config import Settings
from .discussions import Discussions
from .council import Council, public, RESPONSE_SCHEMA
from .history import History
from .monitor import Monitor
from .runtime import RunRecord, capture


def validate_result(payload):
    if (
        not isinstance(payload, dict)
        or payload.get("status") != "SUCCESS"
        or payload.get("error") not in (None, "")
    ):
        raise ValueError("CLI did not return a successful terminal result")
    response = payload.get("response")
    if not isinstance(response, str) or not response.strip():
        raise ValueError("CLI returned no non-empty response")
    if "conversation_id" in payload and not isinstance(payload["conversation_id"], str):
        raise ValueError("Invalid conversation ID")
    duration = payload.get("duration_seconds")
    if duration is not None and (
        isinstance(duration, bool)
        or not isinstance(duration, (int, float))
        or not math.isfinite(duration)
        or duration < 0
    ):
        raise ValueError("Invalid duration metadata")
    if "num_turns" in payload and (
        type(payload["num_turns"]) is not int or payload["num_turns"] < 0
    ):
        raise ValueError("Invalid turn metadata")
    usage = payload.get("usage")
    if usage is not None:
        if not isinstance(usage, dict) or any(
            k.endswith("tokens") and (type(v) is not int or v < 0) for k, v in usage.items()
        ):
            raise ValueError("Invalid token metadata")
    return response.strip()


class Service:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.history = History(settings.state_dir)
        self.discussions = Discussions(settings.state_dir)
        self.council = Council(self.discussions)
        self.monitor = Monitor(settings.state_dir, settings.port)
        self.active = {}
        self.active_models = {}
        self.closing = False

    async def run(self, prompt, *, discussion=None, council=None, model=None, parallel=False):
        model = model or self.settings.model
        if self.closing:
            raise ValueError("Server is shutting down; new calls are not accepted")
        if not self.settings.enabled:
            raise ValueError(
                "Model execution is disabled. The operator must explicitly set AGY_OBSERVER_ENABLED=1."
            )
        if (
            not isinstance(prompt, str)
            or not prompt.strip()
            or "\x00" in prompt
            or len(prompt.encode("utf-16-le")) // 2 > 16000
        ):
            raise ValueError("Prompt must contain 1..16000 UTF-16 units and no NUL")
        if self.settings.workspace is None or not self.settings.workspace.is_dir():
            raise ValueError("Configure AGY_OBSERVER_WORKSPACE as an existing directory")
        if self.active and not parallel:
            raise ValueError("Another model call is active; parallel paid calls are disabled")
        if model in self.active_models.values() or (parallel and len(self.active) >= 2):
            raise ValueError("Only two distinct council models may run concurrently")
        executable = shutil.which(self.settings.executable)
        if not executable:
            raise ValueError(
                "Configured CLI executable was not found; the desktop IDE is not a CLI substitute"
            )
        if Path(executable).name.lower() not in {"agy", "agy.exe"}:
            raise ValueError("Configured executable must identify agy directly")
        record = RunRecord(self.history.root, prompt, model, "真实对话")
        self.active[record.run_id] = asyncio.current_task()
        self.active_models[record.run_id] = model
        try:
            if discussion or council:
                identifier, message_id = discussion or council
                record.state.update(discussion_id=identifier, discussion_message_id=message_id)
                record.emit("discussion_linked", discussion_id=identifier, message_id=message_id)
                (self.council if council else self.discussions).link(
                    identifier, message_id, record.run_id, model
                )
            self.monitor.start()
            if self.settings.open_browser and not council:
                await self.monitor.open(view="discussions" if discussion or council else None)
                record.emit("monitor_window_requested", url=self.monitor.url)
            command = [
                executable,
                "--output-format",
                "stream-json",
                "--mode",
                "plan",
                "--sandbox",
                "--print-timeout",
                f"{self.settings.cli_timeout}s",
                "--model",
                model,
                "-p",
                prompt,
            ]
            if council:
                command.extend(["--json-schema", json.dumps(RESPONSE_SCHEMA, separators=(",", ":"))])
            _, stderr, code, payload = await capture(
                command,
                cwd=str(self.settings.workspace),
                env=self.settings.cli_environment(),
                timeout=self.settings.outer_timeout,
                limit=self.settings.output_limit,
                record=record,
                stream=True,
            )
            if code != 0:
                raise ValueError("CLI exited unsuccessfully; partial response rejected")
            if any(
                marker in stderr.lower()
                for marker in (
                    b"auto-denied",
                    b"soft-denied",
                    b"soft denied",
                    b"permission denied",
                    b"authentication required",
                )
            ):
                raise ValueError("CLI reported a permission or authentication failure")
            answer = validate_result(payload)
            record.finish(
                "success",
                response=answer,
                exit_code=code,
                conversation_id=payload.get("conversation_id"),
            )
            return dict(
                run_id=record.run_id,
                response=answer,
                model_requested=model,
                conversation_id=payload.get("conversation_id"),
                usage=payload.get("usage"),
                monitor_url=self.monitor.url,
            )
        except BaseException as exc:
            if record.state["state"] not in {"failed", "cancelled", "timeout"}:
                record.finish(
                    "cancelled" if isinstance(exc, asyncio.CancelledError) else "failed",
                    error=type(exc).__name__,
                )
            raise
        finally:
            self.active.pop(record.run_id, None)
            self.active_models.pop(record.run_id, None)

    async def council_round(self, identifier, participants=None, focus="", reply_to=None):
        if self.closing or not self.settings.enabled:
            raise ValueError("Model execution is disabled or server is shutting down")
        if self.active:
            raise ValueError("Wait for the active batch before starting another council round")
        data = self.council.read(identifier)
        if data["models"] != {"gemini": self.settings.model, "claude": self.settings.claude_model}:
            raise ValueError("Council model configuration changed; create a task with the current configuration")
        if self.settings.workspace is None or not self.settings.workspace.is_dir():
            raise ValueError("Configure AGY_OBSERVER_WORKSPACE as an existing directory")
        executable = shutil.which(self.settings.executable)
        if not executable or Path(executable).name.lower() not in {"agy", "agy.exe"}:
            raise ValueError("Configured executable must identify agy directly")
        self.monitor.start()
        if self.settings.open_browser:
            await self.monitor.open(view="discussions", discussion_id=identifier)
        reservations = self.council.reserve(
            identifier,
            participants if participants is not None else ["gemini", "claude"],
            focus,
            reply_to,
        )

        async def contribute(participant, message_id, prompt, model):
            try:
                result = await self.run(
                    prompt, council=(identifier, message_id), model=model, parallel=True
                )
                self.council.finish(identifier, message_id, "success", result["response"])
                return dict(
                    participant=participant,
                    message_id=message_id,
                    run_id=result["run_id"],
                    state="success",
                )
            except BaseException as error:
                state = (
                    "cancelled"
                    if isinstance(error, asyncio.CancelledError)
                    else "timeout" if isinstance(error, asyncio.TimeoutError) else "failed"
                )
                try:
                    self.council.finish(identifier, message_id, state, error=type(error).__name__)
                except (OSError, ValueError):
                    pass
                return dict(
                    participant=participant,
                    message_id=message_id,
                    state=state,
                    error=type(error).__name__,
                )

        outcomes = await asyncio.gather(*(contribute(*item) for item in reservations))
        return dict(
            outcomes=outcomes,
            discussion=public(self.council.read(identifier)),
            discussion_url=self.monitor.discussion_url(identifier),
        )

    async def discussion_turn(self, discussion_id, instruction="", reply_to=None):
        if self.closing or not self.settings.enabled:
            raise ValueError("Model execution is disabled or server is shutting down")
        if self.active:
            raise ValueError("Another model call is active")
        message_id, prompt = self.discussions.reserve(discussion_id, instruction, reply_to)
        try:
            result = await self.run(prompt, discussion=(discussion_id, message_id))
        except BaseException as error:
            state = (
                "cancelled"
                if isinstance(error, asyncio.CancelledError)
                else "timeout" if isinstance(error, asyncio.TimeoutError) else "failed"
            )
            try:
                self.discussions.finish(
                    discussion_id, message_id, state, error=type(error).__name__
                )
            except (OSError, ValueError):
                pass  # Keep the original failure; explicit recovery can reconcile a terminal run.
            raise
        data = self.discussions.finish(discussion_id, message_id, "success", result["response"])
        return dict(
            discussion_id=discussion_id,
            message_id=message_id,
            contribution=data["messages"][-1],
            model_attempts=data["model_attempts"],
            max_model_turns=data["max_model_turns"],
            **result,
        )

    def discussion_recover(self, discussion_id):
        data = self.discussions.read(discussion_id)
        council = data.get("protocol") == "council-v1"
        for message in data["messages"]:
            if message.get("state") != "running":
                if (
                    data.get("protocol") in {"peer-v1", "council-v1"}
                    and message.get("state") == "success"
                    and message.get("format") in {"plain-text", "council-json-partial", "council-json"}
                    and message.get("run_id")
                ):
                    status = self.history.status(message["run_id"])
                    if status["state"] == "success":
                        data = (self.council if council else self.discussions).finish(
                            discussion_id,
                            message["id"],
                            "success",
                            status["response"],
                            normalize_only=True,
                        )
                continue
            run_id = message.get("run_id")
            if run_id in self.active:
                raise ValueError("Cannot recover a call active in this server")
            if run_id:
                status = self.history.status(run_id)
                if status["state"] not in {"success", "failed", "timeout", "cancelled"}:
                    raise ValueError(
                        "Linked run is not terminal; inspect the process before recovery"
                    )
                data = (self.council if council else self.discussions).finish(
                    discussion_id,
                    message["id"],
                    status["state"],
                    status["response"],
                    status.get("error"),
                )
            else:
                data = (self.council if council else self.discussions).finish(
                    discussion_id,
                    message["id"],
                    "interrupted",
                    error="Interrupted before run registration",
                )
        return public(data)

    def cancel(self, run_id):
        task = self.active.get(run_id)
        if task is None:
            return dict(cancel_requested=False, reason="No active call with this run ID")
        task.cancel()
        return dict(cancel_requested=True, run_id=run_id)

    async def close(self):
        self.closing = True
        tasks = list(self.active.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await asyncio.to_thread(self.monitor.close)
