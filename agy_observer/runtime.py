"""Local bounded process capture and append-only public-event history (stdlib only)."""

from __future__ import annotations

import asyncio
import codecs
import json
import math
import os
import signal
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path


def utc():
    return datetime.now(timezone.utc).isoformat()


def strict_json(text):
    def unique(pairs):
        obj = {}
        for key, value in pairs:
            if key in obj:
                raise ValueError("duplicate JSON key")
            obj[key] = value
        return obj

    def constant(value):
        raise ValueError("non-finite JSON number")

    def number(value):
        parsed = float(value)
        if not math.isfinite(parsed):
            raise ValueError("non-finite JSON number")
        return parsed

    return json.loads(text, object_pairs_hook=unique, parse_constant=constant, parse_float=number)


class RunRecord:
    def __init__(self, root, prompt, model, kind="live", run_id=None):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.run_id = run_id or f"{datetime.now(timezone.utc):%Y%m%dT%H%M%S}-{uuid.uuid4().hex[:8]}"
        self.path = self.root / self.run_id
        self.path.mkdir()  # exclusive; a run must never silently overwrite history
        self._snapshot_at = 0.0
        self.state = dict(
            run_id=self.run_id,
            kind=kind,
            model_requested=model,
            task=prompt,
            state="created",
            started_utc=utc(),
            conversation_id=None,
            exit_code=None,
            response="",
            warnings="",
            last_event_utc=utc(),
        )
        self.emit("created")

    def emit(self, event, **data):
        row = dict(run_id=self.run_id, time_utc=utc(), event=event, **data)
        with (self.path / "events.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
        self.state["last_event_utc"] = row["time_utc"]
        self.state["last_event"] = event
        now = time.monotonic()
        if event == "cli_step_update" and now - self._snapshot_at < 0.1:
            return  # Preserve every event; coalesce full-response snapshots to at most 10Hz.
        temp = self.path / "status.tmp"
        temp.write_text(json.dumps(self.state, ensure_ascii=False, indent=2), encoding="utf-8")
        from .storage import retry_permission

        try:
            retry_permission(lambda: os.replace(temp, self.path / "status.json"))
        finally:
            temp.unlink(missing_ok=True)
        self._snapshot_at = now

    def finish(self, state, **data):
        self.state.update(state=state, finished_utc=utc(), **data)
        self.emit("terminal", state=state, exit_code=self.state["exit_code"])
        (self.path / "transcript.md").write_text(
            f"# {self.run_id}\n\n类别：{self.state['kind']}\n\n请求模型：{self.state['model_requested']}\n\n"
            f"会话：{self.state['conversation_id']}\n\n终态：{state}；退出码：{self.state['exit_code']}\n\n"
            f"## 任务\n\n{self.state['task']}\n\n## 回复\n\n{self.state['response']}\n\n"
            f"## 警告\n\n{self.state['warnings']}\n",
            encoding="utf-8",
        )


class CaptureError(RuntimeError):
    pass


async def stop_group(process):
    """Only target the process group created by this invocation, never name-wide kills."""
    if os.name == "posix":
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.killpg(process.pid, sig)
            except ProcessLookupError:
                break
            await asyncio.sleep(0.15)
    else:
        # taskkill /T is scoped to the exact newly created PID. No shell or image-name kill.
        # If the parent has already exited, Windows may no longer find descendants.
        killer = await asyncio.create_subprocess_exec(
            "taskkill.exe",
            "/PID",
            str(process.pid),
            "/T",
            "/F",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            creationflags=0x08000000,
        )
        await asyncio.wait_for(killer.wait(), 3)
    try:
        await asyncio.wait_for(process.wait(), 3)
    except asyncio.TimeoutError as exc:
        raise CaptureError("process cleanup did not complete") from exc


async def capture(
    command, *, cwd, env, timeout=40, limit=2 * 1024 * 1024, record=None, stream=False
):
    """Drain both pipes concurrently; cap each during reading; bound the entire pipe lifetime."""
    options = {"start_new_session": True} if os.name == "posix" else {"creationflags": 0x08000000}
    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            cwd=cwd,
            env=env,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            **options,
        )
    except (OSError, asyncio.CancelledError) as exc:
        if record:
            record.finish(
                "cancelled" if isinstance(exc, asyncio.CancelledError) else "failed",
                error=type(exc).__name__,
            )
        raise
    totals = {"stdout": 0, "stderr": 0}
    result_payloads = []
    stream_started = False

    async def drain(pipe, name):
        nonlocal stream_started
        chunks, pending = [], ""
        decoder = codecs.getincrementaldecoder("utf-8")(
            "strict" if stream and name == "stdout" else "replace"
        )
        while True:
            block = await pipe.read(4096)
            if not block:
                break
            totals[name] += len(block)
            if totals[name] > limit:
                raise CaptureError(f"{name} exceeded {limit} bytes")
            chunks.append(block)
            text = decoder.decode(block)
            if record and name == "stderr":
                record.state["warnings"] += text
                record.emit("stderr", text=text)
            if stream and name == "stdout":
                pending += text
                while "\n" in pending:
                    line, pending = pending.split("\n", 1)
                    if line.strip():
                        event = strict_json(line)
                        if not isinstance(event, dict):
                            raise CaptureError("stream event must be an object")
                        label = event.get("event")
                        if label not in {"init", "step_update", "result"}:
                            raise CaptureError("unrecognized stream event")
                        if result_payloads:
                            raise CaptureError("event after terminal result")
                        if label == "init":
                            if stream_started:
                                raise CaptureError("duplicate init event")
                            stream_started = True
                        elif not stream_started:
                            raise CaptureError("stream event before init")
                        if label == "step_update" and not isinstance(
                            event.get("step_update"), dict
                        ):
                            raise CaptureError("invalid stream step")
                        if label == "init" and not isinstance(event.get("init", {}), dict):
                            raise CaptureError("invalid stream init")
                        if label == "result":
                            result_payloads.append(event.get("result"))
                        if record:
                            # Never persist private thinking or arbitrary tool parameter payloads.
                            step = event.get("step_update", {})
                            public = {
                                k: step[k]
                                for k in ("step_index", "state", "step_type", "tool_name")
                                if k in step
                            }
                            if step.get("step_type") == "agent_response":
                                delta = step.get("text_delta", "")
                                if isinstance(delta, str):
                                    public["text_delta"] = delta
                                    record.state["response"] += delta
                            if label == "init":
                                record.state["conversation_id"] = event.get("conversation_id")
                                public = {
                                    k: event.get("init", {}).get(k)
                                    for k in ("model", "permission_mode")
                                }
                            record.emit("cli_" + str(label), **public)
        tail = decoder.decode(b"", final=True)
        if record and name == "stderr" and tail:
            record.state["warnings"] += tail
            record.emit("stderr", text=tail)
        if stream and name == "stdout":
            pending += tail
        if stream and name == "stdout" and pending.strip():
            raise CaptureError("stream ended without a complete NDJSON line")
        return b"".join(chunks)

    async def heartbeat():
        while True:
            await asyncio.sleep(2)
            if record:
                record.emit("heartbeat", bytes_received=totals.copy())

    tasks = [
        asyncio.create_task(drain(process.stdout, "stdout")),
        asyncio.create_task(drain(process.stderr, "stderr")),
        asyncio.create_task(process.wait()),
    ]
    pulse = asyncio.create_task(heartbeat())
    joined = asyncio.gather(*tasks)
    try:
        if record:
            record.state["state"] = "running"
            record.emit("spawn", pid=process.pid)
        stdout, stderr, code = await asyncio.wait_for(joined, timeout)
        if stream and len(result_payloads) != 1:
            raise CaptureError("stream must contain exactly one terminal result")
        if record:
            record.state["exit_code"] = code
            record.emit("process_exit", exit_code=code, bytes_received=totals)
        return stdout, stderr, code, result_payloads[0] if stream else None
    except BaseException as exc:
        for task in tasks:
            task.cancel()

        async def cleanup():
            errors = []
            try:
                await stop_group(process)
            except Exception as error:
                errors.append(type(error).__name__)
            await asyncio.gather(*tasks, return_exceptions=True)
            await asyncio.gather(joined, return_exceptions=True)

            async def discard(pipe):
                while await pipe.read(4096):
                    pass

            # Never collect unbounded residual output, including output from a detached writer.
            try:
                await asyncio.wait_for(
                    asyncio.gather(discard(process.stdout), discard(process.stderr)), 3
                )
            except Exception as error:
                errors.append(type(error).__name__)
            if record:
                state = (
                    "cancelled"
                    if isinstance(exc, asyncio.CancelledError)
                    else "timeout" if isinstance(exc, asyncio.TimeoutError) else "failed"
                )
                try:
                    record.finish(
                        state,
                        exit_code=process.returncode,
                        error=type(exc).__name__,
                        cleanup_error=", ".join(errors) or None,
                    )
                except OSError:
                    # A failed disk must not prevent process cleanup or replace the original error.
                    pass

        cleanup_task = asyncio.create_task(cleanup())
        while True:
            try:
                await asyncio.shield(cleanup_task)
                break
            except asyncio.CancelledError:
                if cleanup_task.done():
                    cleanup_task.result()
                    break
        raise
    finally:
        pulse.cancel()
        await asyncio.gather(pulse, return_exceptions=True)
