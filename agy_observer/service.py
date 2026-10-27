"""One MCP call maps to one CLI process; no retries, login automation, or shell arguments."""

import asyncio
import math
import json
import os
import shutil
from pathlib import Path

from .config import Settings
from .channels import Endpoint, stream_parser
from .discussions import Discussions
from .council import Council, public, RESPONSE_SCHEMA
from .history import History
from .monitor import Monitor
from .runtime import CaptureError, RunRecord, capture


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
        self.active_routes = {}
        self.batch_task = None
        self._batch_token = None
        self.closing = False

    def endpoint(self, participant="gemini", channel=None, model=None):
        endpoint = self.settings.endpoint(participant, channel, model)
        executable = shutil.which(self.settings.channel_executable(endpoint.channel))
        names = {
            "antigravity": {"agy", "agy.exe"},
            "claude_code": {"claude", "claude.exe"},
            "codex_cli": {"codex", "codex.exe"},
        }
        if not executable:
            raise ValueError(
                f"{endpoint.channel} CLI is missing; install/login that CLI in the MCP environment. No channel fallback."
            )
        if Path(executable).name.lower() not in names[endpoint.channel]:
            raise ValueError(
                f"Configure the native {endpoint.channel} executable directly (no shell/.cmd wrapper)"
            )
        if os.name == "posix" and str(executable).lower().endswith(".exe"):
            raise ValueError(
                "Cross-OS CLI forwarding is unsupported; install this CLI in the MCP's Linux/WSL environment for bounded cancellation"
            )
        return endpoint, executable

    def create_council(
        self,
        goal,
        facts="",
        max_turns_per_model=2,
        roles=None,
        project=None,
        participants=None,
        routes=None,
    ):
        peers = participants if participants is not None else ["gpt", "gemini", "claude"]
        if (
            not isinstance(peers, list)
            or len(set(peers)) != len(peers)
            or not 2 <= len(peers) <= 4
            or "gpt" not in peers
        ):
            raise ValueError("Choose 2..4 distinct participants including client gpt")
        remote = set(peers) - {"gpt"}
        if not remote <= {"gemini", "claude", "codex"}:
            raise ValueError("Unknown participant")
        if routes is not None and (not isinstance(routes, dict) or not set(routes) <= remote):
            raise ValueError("Routes must belong to selected remote participants")
        endpoints = {}
        for peer in peers:
            if peer == "gpt":
                continue
            spec = (routes or {}).get(peer, {})
            if peer in (routes or {}) and (
                not isinstance(spec, dict) or set(spec) != {"channel", "model"}
            ):
                raise ValueError("Each route needs channel and model")
            endpoints[peer] = self.settings.endpoint(peer, **spec)
        return self.council.create(
            goal,
            facts,
            {p: e.model for p, e in endpoints.items()},
            max_turns_per_model,
            roles=roles,
            project=project,
            participants=peers,
            routes={p: e.public() for p, e in endpoints.items()},
        )

    async def run(
        self,
        prompt,
        *,
        discussion=None,
        council=None,
        model=None,
        participant="gemini",
        channel=None,
        parallel=False,
        _batch_token=None,
    ):
        if self.closing:
            raise ValueError("Server is shutting down; new calls are not accepted")
        if not self.settings.enabled:
            raise ValueError(
                "Model execution is disabled. The operator must explicitly set AGY_OBSERVER_ENABLED=1."
            )
        if self.batch_task is not None and _batch_token is not self._batch_token:
            raise ValueError("Wait for the active batch before starting another call")
        prompt_limit = 32000 if discussion or council else 16000
        if (
            not isinstance(prompt, str)
            or not prompt.strip()
            or "\x00" in prompt
            or len(prompt.encode("utf-16-le")) // 2 > prompt_limit
        ):
            raise ValueError(f"Prompt must contain 1..{prompt_limit} UTF-16 units and no NUL")
        if self.settings.workspace is None or not self.settings.workspace.is_dir():
            raise ValueError("Configure AGY_OBSERVER_WORKSPACE as an existing directory")
        if self.active and not parallel:
            raise ValueError("Another model call is active; parallel paid calls are disabled")
        endpoint, executable = self.endpoint(participant, channel, model)
        model = endpoint.model
        route_key = (endpoint.channel, model)
        if route_key in self.active_routes.values() or (parallel and len(self.active) >= 2):
            raise ValueError("Only two distinct channel/model routes may run concurrently")
        record = RunRecord(self.history.root, prompt, model, "真实对话")
        record.state.update(participant=participant, channel=endpoint.channel, source="model")
        record.emit(
            "route_selected",
            participant=participant,
            channel=endpoint.channel,
            model_requested=model,
        )
        self.active[record.run_id] = asyncio.current_task()
        self.active_models[record.run_id] = model
        self.active_routes[record.run_id] = route_key
        try:
            if discussion or council:
                identifier, message_id = discussion or council
                record.state.update(discussion_id=identifier, discussion_message_id=message_id)
                record.emit("discussion_linked", discussion_id=identifier, message_id=message_id)
                (self.council if council else self.discussions).link(
                    identifier, message_id, record.run_id, model, endpoint.channel, participant
                )
            self.monitor.start()
            if self.settings.open_browser and not council:
                await self.monitor.open(view="discussions" if discussion or council else None)
                record.emit("monitor_window_requested", url=self.monitor.url)
            schema = RESPONSE_SCHEMA if council else None
            if council and endpoint.channel == "codex_cli":
                schema = record.path / "response-schema.json"
                schema.write_text(json.dumps(RESPONSE_SCHEMA), encoding="utf-8")
            command = endpoint.command(executable, prompt, self.settings.cli_timeout, schema)
            _, stderr, code, payload = await capture(
                command,
                cwd=str(self.settings.workspace),
                env=self.settings.cli_environment(endpoint.channel),
                timeout=(
                    self.settings.outer_timeout
                    if endpoint.channel == "antigravity"
                    else self.settings.cli_timeout
                ),
                limit=self.settings.output_limit,
                record=record,
                stream=True,
                parser=stream_parser(endpoint.channel),
            )
            if code != 0:
                raise ValueError(
                    f"{endpoint.channel} CLI exited unsuccessfully (run {record.run_id}); check that CLI login/permissions. No retry or fallback."
                )
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
            try:
                answer = validate_result(payload)
            except ValueError as error:
                raise ValueError(
                    f"{endpoint.channel} did not return a successful response (run {record.run_id}); check CLI login, account model access and permissions. No fallback/retry."
                ) from error
            if (
                participant == "claude"
                and record.state.get("model_reported")
                and "opus" not in record.state["model_reported"].lower()
            ):
                raise ValueError("Claude reported a non-Opus model; result rejected")
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
                participant=participant,
                channel=endpoint.channel,
                source="model",
                state="success",
                conversation_id=payload.get("conversation_id"),
                usage=payload.get("usage"),
                monitor_url=self.monitor.url,
            )
        except BaseException as exc:
            if record.state["state"] not in {"failed", "cancelled", "timeout"}:
                record.finish(
                    "cancelled" if isinstance(exc, asyncio.CancelledError) else "failed",
                    error=type(exc).__name__,
                    error_message=str(exc)[:500],
                )
            if isinstance(exc, (CaptureError, ValueError)):
                raise ValueError(
                    f"{endpoint.channel} request failed (run {record.run_id}): {exc}. "
                    "Check this CLI's login, model access, permissions and output version; no channel/model fallback or retry."
                ) from exc
            raise
        finally:
            self.active.pop(record.run_id, None)
            self.active_models.pop(record.run_id, None)
            self.active_routes.pop(record.run_id, None)

    async def council_round(self, identifier, participants=None, focus="", reply_to=None):
        if self.closing or not self.settings.enabled:
            raise ValueError("Model execution is disabled or server is shutting down")
        if self.active or self.batch_task is not None:
            raise ValueError("Wait for the active batch before starting another council round")
        # No await before claiming admission: even browser startup belongs to this batch.
        self.batch_task = asyncio.current_task()
        self._batch_token = object()
        try:
            return await self._council_round(identifier, participants, focus, reply_to)
        finally:
            self.batch_task = None
            self._batch_token = None

    async def _council_round(self, identifier, participants, focus, reply_to):
        data = self.council.read(identifier)
        chosen = participants if participants is not None else list(data["models"])[:2]
        if (
            not isinstance(chosen, list)
            or not chosen
            or len(chosen) > 2
            or len(set(chosen)) != len(chosen)
            or not set(chosen) <= set(data["models"])
        ):
            raise ValueError("Select up to two remote participants belonging to this task")
        if "routes" not in data and data["models"] != {
            "gemini": self.settings.model,
            "claude": self.settings.claude_model,
        }:
            raise ValueError(
                "Council model configuration changed; create a task with the current configuration"
            )
        endpoints = {}
        for peer in chosen:
            spec = data.get("routes", {}).get(
                peer, dict(channel="antigravity", model=data["models"][peer])
            )
            endpoints[peer], _ = self.endpoint(peer, **spec)
        if len({(e.channel, e.model) for e in endpoints.values()}) != len(chosen):
            raise ValueError("A batch requires distinct channel/model routes")
        if self.settings.workspace is None or not self.settings.workspace.is_dir():
            raise ValueError("Configure AGY_OBSERVER_WORKSPACE as an existing directory")
        self.monitor.start()
        if self.settings.open_browser:
            await self.monitor.open(view="discussions", discussion_id=identifier)
        reservations = self.council.reserve(
            identifier,
            chosen,
            focus,
            reply_to,
        )

        async def contribute(participant, message_id, prompt, model):
            result = None
            try:
                result = await self.run(
                    prompt,
                    council=(identifier, message_id),
                    model=model,
                    participant=participant,
                    channel=endpoints[participant].channel,
                    parallel=True,
                    _batch_token=self._batch_token,
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
                    error_message=str(error)[:500],
                    **({"run_id": result["run_id"], "recovery_available": True} if result else {}),
                )

        outcomes = await asyncio.gather(*(contribute(*item) for item in reservations))
        return dict(
            outcomes=outcomes,
            discussion=public(self.council.read(identifier)),
            discussion_url=self.monitor.discussion_url(identifier),
        )

    async def discussion_turn(
        self,
        discussion_id,
        instruction="",
        reply_to=None,
        participant="gemini",
        channel=None,
        model=None,
    ):
        if self.closing or not self.settings.enabled:
            raise ValueError("Model execution is disabled or server is shutting down")
        if self.active or self.batch_task is not None:
            raise ValueError("Another model call is active")
        endpoint, _ = self.endpoint(participant, channel, model)
        message_id, prompt = self.discussions.reserve(
            discussion_id, instruction, reply_to, participant=participant
        )
        try:
            result = await self.run(
                prompt,
                discussion=(discussion_id, message_id),
                participant=participant,
                channel=endpoint.channel,
                model=endpoint.model,
            )
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

        def check_provenance(status, message):
            # Old files without channel metadata keep their legacy recovery semantics.
            if status.get("channel") is not None or (
                "routes" in data and message.get("source") == "model"
            ):
                if (
                    status.get("discussion_id") != discussion_id
                    or status.get("discussion_message_id") != message["id"]
                    or status.get("model_requested") != message.get("model_requested")
                    or status.get("channel") != message.get("channel")
                    or status.get("participant") != message.get("participant")
                    or status.get("source") != "model"
                ):
                    raise ValueError(
                        "Linked run participant/channel/model does not match this message"
                    )

        for message in data["messages"]:
            run_id = message.get("run_id")
            if run_id and message.get("state") in {"failed", "timeout", "cancelled", "interrupted"}:
                if run_id in self.active:
                    raise ValueError("Cannot recover a call active in this server")
                status = self.history.status(run_id)
                check_provenance(status, message)
                if status["state"] == "success":
                    if (
                        status.get("discussion_id") != discussion_id
                        or status.get("discussion_message_id") != message["id"]
                        or status.get("model_requested") != message.get("model_requested")
                        or (
                            message.get("channel") is not None
                            and status.get("channel") != message["channel"]
                        )
                        or (
                            message.get("participant") is not None
                            and status.get("participant") != message["participant"]
                        )
                    ):
                        raise ValueError(
                            "Successful run does not match the failed discussion message"
                        )
                    data = (self.council if council else self.discussions).finish(
                        discussion_id,
                        message["id"],
                        "success",
                        status["response"],
                        recovered_run_id=run_id,
                    )
                continue
            if message.get("state") != "running":
                if (
                    data.get("protocol") in {"peer-v1", "council-v1"}
                    and message.get("state") == "success"
                    and message.get("format")
                    in {"plain-text", "council-json-partial", "council-json"}
                    and message.get("run_id")
                ):
                    status = self.history.status(message["run_id"])
                    check_provenance(status, message)
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
                check_provenance(status, message)
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
        if self.batch_task is not None:
            tasks.append(self.batch_task)
        tasks = list({task for task in tasks if task is not asyncio.current_task()})
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await asyncio.to_thread(self.monitor.close)
