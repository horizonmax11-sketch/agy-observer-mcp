"""Offline CLI protocols, explicit provenance and mixed-channel project workflows."""

import asyncio
from dataclasses import replace
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from agy_observer.channels import Endpoint, stream_parser
from agy_observer.config import Settings
from agy_observer.council import public
from agy_observer.project import ProjectWorkflow
from agy_observer.runtime import CaptureError, RunRecord, capture
from agy_observer.service import Service, validate_result

PROFILE = dict(
    stage="理解",
    materials="synthetic fixture",
    success_criteria="checks pass",
    constraints="offline",
    unknowns="none",
)


def envelope(text="synthetic"):
    return json.dumps(
        dict(
            message=text,
            kind="observation",
            references=[],
            answers_to=[],
            evidence="",
            change_reason="",
            question=None,
        )
    )


def events(channel, text="public answer"):
    if channel == "claude_code":
        return [
            dict(type="system", subtype="init", session_id="SIMULATED", model="claude-opus-test"),
            dict(
                type="stream_event",
                event=dict(delta=dict(type="thinking_delta", thinking="PRIVATE-FIXTURE")),
            ),
            dict(type="stream_event", event=dict(delta=dict(type="text_delta", text=text))),
            dict(
                type="assistant",
                message=dict(
                    id="m1",
                    model="claude-opus-test",
                    content=[
                        dict(type="text", text=text),
                        dict(type="thinking", thinking="PRIVATE-FIXTURE"),
                    ],
                ),
            ),
            dict(
                type="result",
                subtype="success",
                is_error=False,
                result=text,
                session_id="SIMULATED",
                usage=dict(output_tokens=2),
            ),
        ]
    if channel == "codex_cli":
        return [
            dict(type="thread.started", thread_id="SIMULATED"),
            dict(type="turn.started"),
            dict(
                type="item.completed", item=dict(id="r", type="reasoning", text="PRIVATE-FIXTURE")
            ),
            dict(type="item.completed", item=dict(id="m", type="agent_message", text=text)),
            dict(type="turn.completed", usage=dict(output_tokens=2)),
        ]
    return [
        dict(event="init", init=dict(model="gemini-test")),
        dict(
            event="step_update",
            step_update=dict(step_type="thinking", text_delta="PRIVATE-FIXTURE"),
        ),
        dict(event="step_update", step_update=dict(step_type="agent_response", text_delta=text)),
        dict(event="result", result=dict(status="SUCCESS", response=text)),
    ]


class RouteTests(unittest.TestCase):
    def test_separate_participant_channel_and_model(self):
        settings = Settings(participants={"claude": dict(channel="claude_code", model="opus")})
        self.assertEqual(settings.endpoint("claude").channel, "claude_code")
        self.assertEqual(settings.endpoint("gemini").channel, "antigravity")
        self.assertEqual(settings.endpoint("codex").channel, "codex_cli")
        for arguments in [
            ("gpt", "codex_cli", "gpt-test"),
            ("gemini", "claude_code", "opus"),
            ("claude", "antigravity", "sonnet"),
        ]:
            with self.subTest(arguments=arguments), self.assertRaises(ValueError):
                Endpoint(*arguments)
        with self.assertRaises(ValueError):
            Settings().endpoint("claude", channel="claude_code")
        with self.assertRaises(ValueError):
            Settings().endpoint("codex", model="")

    def test_distinct_safe_commands(self):
        for peer, channel, model in [
            ("gemini", "antigravity", "gemini-test"),
            ("claude", "claude_code", "opus"),
            ("codex", "codex_cli", "gpt-test"),
        ]:
            command = Endpoint(peer, channel, model).command(peer, "prompt", 30)
            self.assertFalse(
                any(
                    "dangerously" in flag
                    or flag in ("--yolo", "--full-auto", "acceptEdits", "bypassPermissions")
                    for flag in command
                )
            )
            if channel == "claude_code":
                self.assertEqual(command[command.index("--tools") + 1], "")
                self.assertIn("--include-partial-messages", command)
                self.assertIn("plan", command)
            if channel == "codex_cli":
                self.assertIn("read-only", command)
                self.assertIn("on-request", command)
                self.assertIn("mcp_servers.agy_observer.enabled=false", command)

    def test_cli_environment_does_not_leak_agy_home_or_tokens_to_official_channels(self):
        with patch.dict(
            os.environ,
            dict(
                AGY_OBSERVER_CLI_HOME="/private-agy",
                HOME="/normal-home",
                OPENAI_API_KEY="SECRET-FIXTURE",
                AGY_OBSERVER_CODEX_HOME="/existing-codex",
            ),
            clear=True,
        ):
            settings = Settings()
            self.assertEqual(settings.cli_environment()["HOME"], "/private-agy")
            self.assertEqual(settings.cli_environment("codex_cli")["HOME"], "/normal-home")
            self.assertEqual(settings.cli_environment("codex_cli")["CODEX_HOME"], "/existing-codex")
            self.assertNotIn("OPENAI_API_KEY", settings.cli_environment("codex_cli"))

    def test_error_terminal_is_not_recovered_as_success(self):
        for channel in ("claude_code", "codex_cli"):
            parser = stream_parser(channel)
            fixture = events(channel)
            if channel == "claude_code":
                fixture[-1]["is_error"] = True
            else:
                fixture[-1]["type"] = "turn.failed"
            for event in fixture:
                parser.feed(event)
            with self.assertRaises(ValueError):
                validate_result(parser.finish())

    def test_no_terminal_duplicate_terminal_and_non_opus_are_rejected(self):
        for channel in ("antigravity", "claude_code", "codex_cli"):
            parser = stream_parser(channel)
            fixture = events(channel)
            for event in fixture[:-1]:
                parser.feed(event)
            with self.assertRaises(ValueError):
                parser.finish()
            parser.feed(fixture[-1])
            with self.assertRaises(ValueError):
                parser.feed(fixture[-1])
        parser = stream_parser("claude_code")
        parser.feed(events("claude_code")[0])
        with self.assertRaises(ValueError):
            parser.feed(
                dict(type="assistant", message=dict(model="claude-sonnet-test", content=[]))
            )


class CaptureTests(unittest.IsolatedAsyncioTestCase):
    async def test_streams_keep_public_text_and_strip_thinking(self):
        with tempfile.TemporaryDirectory() as folder:
            for channel in ("antigravity", "claude_code", "codex_cli"):
                record = RunRecord(folder, "synthetic", "test", kind="离线模拟")
                code = (
                    "import json; events="
                    + repr(events(channel))
                    + "; [print(json.dumps(e),flush=True) for e in events]"
                )
                _, _, status, payload = await capture(
                    [sys.executable, "-c", code],
                    cwd=folder,
                    env=os.environ.copy(),
                    timeout=5,
                    record=record,
                    stream=True,
                    parser=stream_parser(channel),
                )
                self.assertEqual(status, 0)
                self.assertEqual(validate_result(payload), "public answer")
                self.assertEqual(record.state["response"], "public answer")
                self.assertNotIn("PRIVATE-FIXTURE", (record.path / "events.jsonl").read_text())

    async def test_each_channel_timeout_and_cancel_clean_up(self):
        with tempfile.TemporaryDirectory() as folder:
            for channel in ("antigravity", "claude_code", "codex_cli"):
                for cancel in (False, True):
                    with self.subTest(channel=channel, cancel=cancel):
                        record = RunRecord(folder, "synthetic", "test", kind="离线模拟")
                        task = asyncio.create_task(
                            capture(
                                [sys.executable, "-c", "import time;time.sleep(30)"],
                                cwd=folder,
                                env=os.environ.copy(),
                                timeout=0.2 if not cancel else 5,
                                record=record,
                                stream=True,
                                parser=stream_parser(channel),
                            )
                        )
                        if cancel:
                            await asyncio.sleep(0.2)
                            task.cancel()
                        with self.assertRaises(
                            asyncio.CancelledError if cancel else asyncio.TimeoutError
                        ):
                            await task
                        self.assertEqual(
                            record.state["state"], "cancelled" if cancel else "timeout"
                        )


class MixedCouncilTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.service = Service(
            Settings(
                state_dir=Path(self.temp.name),
                workspace=Path(self.temp.name),
                enabled=True,
                open_browser=False,
            )
        )
        self.addAsyncCleanup(self.service.close)

    def create(self, participants=None):
        participants = participants or ["gpt", "claude", "codex"]
        return self.service.create_council(
            "synthetic",
            project=PROFILE,
            participants=participants,
            routes=(
                {"claude": dict(channel="claude_code", model="opus")}
                if "claude" in participants
                else None
            ),
        )

    async def test_mixed_channels_share_dynamic_roles_plan_and_real_run_provenance(self):
        created = self.create()
        identifier = created["discussion_id"]
        self.service.council.note(identifier, "client GPT understanding", "initial")
        commands = []

        async def fake(command, **kwargs):
            commands.append(command)
            return b"", b"", 0, dict(status="SUCCESS", response=envelope("external " + command[0]))

        with (
            patch("agy_observer.service.shutil.which", side_effect=lambda name: name),
            patch("agy_observer.service.capture", side_effect=fake),
        ):
            result = await self.service.council_round(identifier)
        self.assertEqual(len(commands), 2)
        self.assertEqual({c[0] for c in commands}, {"claude", "codex"})
        codex = next(c for c in commands if c[0] == "codex")
        self.assertTrue(Path(codex[codex.index("--output-schema") + 1]).is_file())
        revealed = self.service.council.reveal(identifier)
        self.assertEqual(
            revealed["initial_status"], dict(gpt="success", claude="success", codex="success")
        )
        specs = {
            p: dict(role=p, focus="evidence", output="checks", reason="current gap")
            for p in ("gpt", "claude", "codex")
        }
        ProjectWorkflow(self.service.council).assign(
            identifier, specs, "work", "from initial evidence", [1, 2, 3], 0
        )
        plan = self.service.council.note(
            identifier, "one shared plan", "plan", references=[1, 2, 3], expected_plan_version=0
        )
        self.assertEqual(plan["plan_version"], 1)
        self.assertEqual(set(plan["project"]["role_versions"][0]["prompts"]), set(specs))
        for message in plan["messages"][:3]:
            if message["author"] == "gpt":
                self.assertEqual(message["channel"], "client")
                self.assertNotIn("run_id", message)
            else:
                run = self.service.history.status(message["run_id"])
                self.assertEqual(run["channel"], message["channel"])
                self.assertEqual(run["participant"], message["author"])
                self.assertEqual(run["model_requested"], message["model_requested"])
        raw = self.service.council.read(identifier)
        raw["messages"][2]["source"] = "client"
        with self.service.discussions.writing():
            self.service.discussions.save(raw)
        with self.assertRaises(ValueError):
            self.service.council.read(identifier)

    async def test_four_participants_optional_batches_and_snapshot_channels(self):
        created = self.create(["gpt", "gemini", "claude", "codex"])
        identifier = created["discussion_id"]
        self.service.council.note(identifier, "initial", "initial")
        self.service.settings = replace(
            self.service.settings,
            participants={"claude": dict(channel="antigravity", model="claude-opus-other")},
        )
        commands = []

        async def fake(command, **kwargs):
            commands.append(command)
            return b"", b"", 0, dict(status="SUCCESS", response=envelope())

        with (
            patch("agy_observer.service.shutil.which", side_effect=lambda name: name),
            patch("agy_observer.service.capture", side_effect=fake),
        ):
            await self.service.council_round(identifier, ["claude"])
            self.assertEqual(len(commands), 1)
            self.assertEqual(
                commands[0][0], "claude"
            )  # Uses the task snapshot, not silently switched AGY.
            await self.service.council_round(identifier, ["gemini", "codex"])
        data = self.service.council.reveal(identifier)
        self.assertEqual(set(data["initial_status"]), {"gpt", "gemini", "claude", "codex"})
        specs = {
            p: dict(role=p, focus="gap", output="evidence", reason="source") for p in data["roles"]
        }
        ProjectWorkflow(self.service.council).assign(
            identifier, specs, "work", "all sources", [1, 2, 3, 4], 0
        )
        self.assertEqual(len(self.service.council.read(identifier)["roles"]), 4)

    async def test_missing_cli_rejects_before_reserving_any_attempt(self):
        identifier = self.create()["discussion_id"]
        with (
            patch("agy_observer.service.shutil.which", return_value=None),
            patch("agy_observer.service.capture") as spawn,
        ):
            with self.assertRaisesRegex(ValueError, "missing"):
                await self.service.council_round(identifier, ["claude"])
        self.assertEqual(self.service.council.read(identifier)["model_attempts"], 0)
        spawn.assert_not_called()

    async def test_error_result_has_one_attempt_no_retry_and_channel_in_history(self):
        identifier = self.create()["discussion_id"]

        async def failure(*args, **kwargs):
            return b"", b"authentication required", 1, dict(status="ERROR", response="partial")

        with (
            patch("agy_observer.service.shutil.which", side_effect=lambda name: name),
            patch("agy_observer.service.capture", side_effect=failure) as calls,
        ):
            result = await self.service.council_round(identifier, ["codex"])
        self.assertEqual(calls.call_count, 1)
        self.assertEqual(result["outcomes"][0]["state"], "failed")
        row = self.service.history.list()[0]
        self.assertEqual(row["channel"], "codex_cli")
        self.assertEqual(row["state"], "failed")
        self.assertEqual(self.service.council.read(identifier)["attempts"]["codex"], 1)

    async def test_two_party_turn_can_use_codex_without_faking_client(self):
        task = self.service.discussions.create("synthetic", "client", "external", 2)

        async def fake(*args, **kwargs):
            return (
                b"",
                b"",
                0,
                dict(
                    status="SUCCESS",
                    response=json.dumps(dict(message="external", question=None, answers_to=[])),
                ),
            )

        with (
            patch("agy_observer.service.shutil.which", side_effect=lambda name: name),
            patch("agy_observer.service.capture", side_effect=fake),
        ):
            result = await self.service.discussion_turn(task["discussion_id"], participant="codex")
        self.assertEqual(result["contribution"]["channel"], "codex_cli")
        self.assertEqual(result["contribution"]["participant"], "codex")
        self.assertEqual(result["contribution"]["source"], "model")

    async def test_recovery_rejects_mismatched_channel_and_keeps_run_unchanged(self):
        identifier = self.create()["discussion_id"]

        async def fake(*args, **kwargs):
            return b"", b"", 0, dict(status="SUCCESS", response=envelope())

        with (
            patch("agy_observer.service.shutil.which", side_effect=lambda name: name),
            patch("agy_observer.service.capture", side_effect=fake),
        ):
            await self.service.council_round(identifier, ["codex"])
        data = self.service.council.read(identifier)
        message = data["messages"][0]
        run = self.service.history.status(message["run_id"])
        data["messages"][0]["participant"] = "claude"
        data["messages"][0]["state"] = "failed"
        with self.service.discussions.writing():
            self.service.discussions.save(data)
        with self.assertRaisesRegex(ValueError, "does not match"):
            self.service.discussion_recover(identifier)
        self.assertEqual(self.service.history.status(message["run_id"]), run)


class MCPChannelTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_stdio_handshake_exposes_routes_without_starting_any_cli(self):
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        with tempfile.TemporaryDirectory() as folder:
            env = {
                k: v
                for k, v in os.environ.items()
                if k.upper()
                in {
                    "PATH",
                    "HOME",
                    "USERPROFILE",
                    "SYSTEMROOT",
                    "WINDIR",
                    "COMSPEC",
                    "TEMP",
                    "TMP",
                    "TMPDIR",
                }
            }
            env.update(
                AGY_OBSERVER_ENABLED="0",
                AGY_OBSERVER_STATE_DIR=folder,
                PYTHONUTF8="1",
                AGY_OBSERVER_PARTICIPANTS=json.dumps(
                    {"claude": dict(channel="claude_code", model="opus")}
                ),
            )
            params = StdioServerParameters(
                command=sys.executable,
                args=["-B", "-m", "agy_observer.server"],
                cwd=str(Path(__file__).resolve().parents[1]),
                env=env,
            )
            with (Path(folder) / "stderr.log").open("w", encoding="utf-8") as log:
                async with stdio_client(params, errlog=log) as (read, write):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        tools = await session.list_tools()
                        run = next(t for t in tools.tools if t.name == "agy_run")
                        self.assertTrue(
                            {"participant", "channel", "model"}
                            <= set(run.inputSchema["properties"])
                        )
                        result = await session.call_tool("agy_info", {})
                        info = json.loads(result.content[0].text)
                        self.assertEqual(info["participants"]["claude"]["channel"], "claude_code")
                        self.assertEqual(info["client_source"], "gpt")
                        created = await session.call_tool(
                            "agy_council_create",
                            dict(
                                goal="synthetic",
                                project=PROFILE,
                                participants=["gpt", "claude", "codex"],
                            ),
                        )
                        self.assertFalse(created.isError)
                        task = json.loads(created.content[0].text)
                        self.assertEqual(task["routes"]["claude"]["model"], "opus")
                        noted = await session.call_tool(
                            "agy_council_note",
                            dict(
                                discussion_id=task["discussion_id"],
                                content="synthetic client",
                                kind="initial",
                            ),
                        )
                        message = json.loads(noted.content[0].text)["messages"][0]
                        self.assertEqual(message["source"], "client")
                        self.assertEqual(message["channel"], "client")
                        denied = await session.call_tool(
                            "agy_council_round",
                            dict(discussion_id=task["discussion_id"], participants=["codex"]),
                        )
                        self.assertTrue(denied.isError)
                        self.assertFalse((Path(folder) / "runs").exists())
