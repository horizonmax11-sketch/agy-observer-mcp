"""One MCP call maps to one CLI process; no retries, login automation, or shell arguments."""

import asyncio
import math
import shutil
from pathlib import Path

from .config import Settings
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
        self.monitor = Monitor(settings.state_dir, settings.port)
        self.active = {}
        self.closing = False

    async def run(self, prompt):
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
        if self.active:
            raise ValueError("Another model call is active; parallel paid calls are disabled")
        executable = shutil.which(self.settings.executable)
        if not executable:
            raise ValueError(
                "Configured CLI executable was not found; the desktop IDE is not a CLI substitute"
            )
        if Path(executable).name.lower() not in {"agy", "agy.exe"}:
            raise ValueError("Configured executable must identify agy directly")
        record = RunRecord(self.history.root, prompt, self.settings.model, "真实对话")
        self.active[record.run_id] = asyncio.current_task()
        try:
            self.monitor.start()
            if self.settings.open_browser:
                await self.monitor.open()
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
                self.settings.model,
                "-p",
                prompt,
            ]
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
                model_requested=self.settings.model,
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
