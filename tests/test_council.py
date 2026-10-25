"""Offline behavioral checks for isolation, concurrency, budgets and shared revisions."""

import asyncio
import http.client
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from agy_observer.config import Settings
from agy_observer.council import Council, public, council_envelope
from agy_observer.discussions import Discussions
from agy_observer.runtime import RunRecord
from agy_observer.service import Service


def envelope(message="synthetic", **extra):
    return json.dumps(
        dict(message=message, kind="observation", references=[], answers_to=[], **extra)
    )


class CouncilTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Discussions(self.temp.name)
        self.council = Council(self.store)
        self.identifier = self.council.create(
            "synthetic goal",
            "original facts",
            {"gemini": "gemini-test", "claude": "claude-test"},
            2,
        )["discussion_id"]

    def initial(self):
        self.council.note(
            self.identifier,
            "GPT initial",
            "initial",
            initial_context="fresh",
            question="initial question?",
        )
        batch = self.council.reserve(self.identifier, ["gemini", "claude"])
        for participant, message_id, _, _ in batch:
            self.council.finish(
                self.identifier, message_id, "success", envelope(participant + " initial")
            )
        return batch

    def test_initial_context_ignores_earlier_results_and_order(self):
        self.council.note(self.identifier, "SECRET WRONG FIRST IDEA", "initial")
        first = self.council.reserve(self.identifier, ["claude"])[0]
        self.council.finish(self.identifier, first[1], "success", envelope("SECRET EARLY RESULT"))
        second = self.council.reserve(self.identifier, ["gemini"])[0]
        self.assertEqual(first[2], second[2])
        self.assertNotIn("SECRET", second[2])
        self.assertIn("original facts", second[2])
        self.assertEqual(
            self.council.read(self.identifier)["messages"][-1]["context"]["included_ids"], []
        )

    def test_sealed_read_and_reveal_gates(self):
        batch = self.initial()
        data = public(self.council.read(self.identifier))
        self.assertEqual(data["open_questions"], [])
        self.assertNotIn("GPT initial", json.dumps(data))
        self.assertTrue(all(m["sealed"] for m in data["messages"]))
        revealed = self.council.reveal(self.identifier)
        self.assertEqual(revealed["phase"], "discuss")
        self.assertEqual(revealed["messages"][0]["initial_context"], "fresh")
        self.assertEqual(len(revealed["open_questions"]), 1)
        self.assertEqual(self.council.reveal(self.identifier), revealed)
        with self.assertRaisesRegex(ValueError, "council tools"):
            self.store.note(self.identifier, "bypass")
        with self.assertRaisesRegex(ValueError, "council tools"):
            self.store.reserve(self.identifier)

    def test_initial_leading_focus_and_missing_peer_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "three"):
            self.council.reveal(self.identifier)
        with self.assertRaisesRegex(ValueError, "leading"):
            self.council.reserve(self.identifier, ["claude"], "agree with GPT")
        with self.assertRaisesRegex(ValueError, "distinct"):
            self.council.reserve(self.identifier, ["gemini", "gemini"])
        self.assertEqual(self.council.read(self.identifier)["model_attempts"], 0)

    def test_incomplete_initial_can_archive_without_reveal(self):
        self.council.note(self.identifier, "sealed idea", "initial")
        result = self.council.note(self.identifier, "Stopped with missing participants", "summary")
        self.assertTrue(result["closed"])
        self.assertNotIn("sealed idea", json.dumps(result))
        with self.assertRaisesRegex(ValueError, "closed"):
            self.council.reveal(self.identifier)

    def test_individual_budget_and_atomic_batch_reservation(self):
        self.initial()
        self.council.reveal(self.identifier)
        request = self.council.reserve(self.identifier, ["gemini"])[0]
        self.council.finish(self.identifier, request[1], "failed", error="synthetic")
        with self.assertRaisesRegex(ValueError, "gemini budget"):
            self.council.reserve(self.identifier, ["claude", "gemini"])
        self.assertEqual(self.council.read(self.identifier)["attempts"], {"gemini": 2, "claude": 1})
        request = self.council.reserve(self.identifier, ["claude"])[0]
        self.council.finish(self.identifier, request[1], "cancelled")
        self.assertEqual(self.council.read(self.identifier)["model_attempts"], 4)

    def test_revision_sources_and_shared_plan_compare_and_swap(self):
        self.initial()
        self.council.reveal(self.identifier)
        with self.assertRaisesRegex(ValueError, "reason"):
            self.council.note(self.identifier, "changed", "revision", [2])
        with self.assertRaisesRegex(ValueError, "grounds"):
            self.council.note(self.identifier, "agree", "support", [2])
        data = self.council.note(
            self.identifier, "shared draft", "plan", [1, 2, 3], expected_plan_version=0
        )
        self.assertEqual(data["plan_version"], 1)
        with self.assertRaisesRegex(ValueError, "version"):
            self.council.note(
                self.identifier, "stale overwrite", "plan", [2], expected_plan_version=0
            )
        data = self.council.note(
            self.identifier,
            "revised draft",
            "plan",
            [2, 3],
            change_reason="new evidence",
            expected_plan_version=1,
        )
        self.assertEqual(data["plan_version"], 2)
        self.assertEqual(data["messages"][-2]["content"], "shared draft")
        data = self.council.note(self.identifier, "budget exhausted, unresolved remains", "summary")
        self.assertTrue(data["closed"])
        self.assertEqual(len(data["open_questions"]), 1)

    def test_invalid_metadata_keeps_body_and_valid_question(self):
        self.initial()
        self.council.reveal(self.identifier)
        batch = self.council.reserve(self.identifier, ["claude", "gemini"])
        for _, message_id, prompt, _ in batch:
            self.assertIn('"author": "GPT"', prompt)
            self.assertNotIn("claude-test", prompt)
            bad = json.dumps(
                dict(message="useful body", kind="revision", references=[99], answers_to=[99], question="new question?")
            )
            self.council.finish(self.identifier, message_id, "success", bad)
        data = self.council.read(self.identifier)
        self.assertEqual(len(data["open_questions"]), 3)
        self.assertEqual(data["messages"][-1]["content"], "useful body")
        self.assertEqual(data["messages"][-1]["answers_to"], [])
        self.assertEqual(data["messages"][-1]["kind"], "observation")
        self.assertIn("answers_to", data["messages"][-1]["protocol_warning"])

    def test_malformed_json_is_not_repaired_by_guessing_quotes(self):
        self.council.note(self.identifier, "initial", "initial")
        item = self.council.reserve(self.identifier, ["claude"])[0]
        raw = '{"message":"say "hello" here","question":"why?"}'
        self.council.finish(self.identifier, item[1], "success", raw)
        message = self.council.read(self.identifier)["messages"][-1]
        self.assertEqual(message["content"], raw)
        self.assertEqual(message["format"], "plain-text")
        self.assertFalse(message.get("question"))

    def test_schema_duplicate_requires_two_identical_complete_objects(self):
        body = envelope("readable", question="why?")
        normalized = json.dumps(json.loads(body), sort_keys=True)
        self.assertEqual(council_envelope("```json\n" + body + "\n```\n" + normalized), json.loads(body))
        self.assertEqual(council_envelope(body + "\n" + normalized), json.loads(body))
        for suffix in (envelope("different"), normalized + "\nextra", "not json"):
            with self.assertRaises(ValueError):
                council_envelope("```json\n" + body + "\n```\n" + suffix)

    def test_nested_malformed_body_is_preserved_with_visible_format_warning(self):
        item = self.council.reserve(self.identifier, ["claude"])[0]
        raw = '{"message":"a "quote" inside","kind":"observation"}'
        self.council.finish(self.identifier, item[1], "success", envelope(raw))
        message = self.council.read(self.identifier)["messages"][-1]
        self.assertEqual(message["content"], raw)
        self.assertEqual(message["format"], "council-json-partial")
        self.assertIn("message", message["protocol_warning"])

    def test_project_roles_activate_after_independent_initials(self):
        roles = {"gpt": "implementation tradeoffs", "gemini": "user journey", "claude": "counterexample design"}
        identifier = self.council.create("role task", "facts", {"gemini": "g", "claude": "c"}, roles=roles)["discussion_id"]
        self.council.note(identifier, "initial", "initial")
        batch = self.council.reserve(identifier, ["gemini", "claude"])
        self.assertEqual(batch[0][2], batch[1][2])
        self.assertNotIn("user journey", batch[0][2])
        for _, mid, _, _ in batch:
            self.council.finish(identifier, mid, "success", envelope())
        self.council.reveal(identifier)
        batch = self.council.reserve(identifier, ["gemini", "claude"])
        self.assertIn("user journey", batch[0][2])
        self.assertNotIn("counterexample design", batch[0][2])
        self.assertIn("counterexample design", batch[1][2])
        for invalid in ({"gpt": "only one"}, {**roles, "claude": "x" * 601}, {**roles, "gemini": ""}):
            with self.assertRaises(ValueError):
                self.council.create("bad roles", "", {"gemini": "g", "claude": "c"}, roles=invalid)

    def test_explicit_answers_from_either_remote_do_not_imply_agreement(self):
        self.initial()
        self.council.reveal(self.identifier)
        item = self.council.reserve(self.identifier, ["claude"])[0]
        body = json.dumps(
            dict(
                message="answer with disagreement", kind="challenge", references=[1], answers_to=[1]
            )
        )
        self.council.finish(self.identifier, item[1], "success", body)
        data = self.council.read(self.identifier)
        self.assertEqual(data["open_questions"], [])
        self.assertNotIn("consensus", data)


class CouncilServiceTests(unittest.IsolatedAsyncioTestCase):
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
        self.identifier = self.service.council.create(
            "synthetic",
            "original",
            {"gemini": self.service.settings.model, "claude": self.service.settings.claude_model},
            2,
        )["discussion_id"]
        self.service.council.note(self.identifier, "GPT synthetic", "initial")

    async def test_concurrent_distinct_models_and_same_initial_snapshot(self):
        started = set()
        both = asyncio.Event()
        prompts = []

        async def capture(command, **kwargs):
            model = command[command.index("--model") + 1]
            prompts.append(command[command.index("-p") + 1])
            self.assertIn("--json-schema", command)
            started.add(model)
            if len(started) == 2:
                both.set()
            await asyncio.wait_for(both.wait(), 2)
            return b"", b"", 0, dict(status="SUCCESS", response=envelope(model))

        with (
            patch("agy_observer.service.shutil.which", return_value="agy"),
            patch("agy_observer.service.capture", side_effect=capture) as calls,
        ):
            result = await self.service.council_round(self.identifier)
        self.assertEqual(calls.call_count, 2)
        self.assertTrue(result["discussion_url"].endswith("?view=discussions&discussion_id=" + self.identifier))
        self.assertEqual(prompts[0], prompts[1])
        self.assertEqual(len(started), 2)
        self.assertEqual([m["state"] for m in result["outcomes"]], ["success", "success"])
        self.assertTrue(all(m["sealed"] for m in result["discussion"]["messages"]))
        self.assertEqual(self.service.active, {})
        self.assertEqual(self.service.active_models, {})
        for m in self.service.council.read(self.identifier)["messages"][1:]:
            self.assertEqual(
                self.service.history.status(m["run_id"])["model_requested"], m["model_requested"]
            )

    async def test_one_failure_preserves_other_result_and_no_retry(self):
        async def capture(command, **kwargs):
            if "claude" in command[command.index("--model") + 1]:
                raise asyncio.TimeoutError()
            await asyncio.sleep(0.02)
            return b"", b"", 0, dict(status="SUCCESS", response=envelope("Gemini survives"))

        with (
            patch("agy_observer.service.shutil.which", return_value="agy"),
            patch("agy_observer.service.capture", side_effect=capture) as calls,
        ):
            result = await self.service.council_round(self.identifier)
        self.assertEqual(calls.call_count, 2)
        self.assertEqual([m["state"] for m in result["outcomes"]], ["success", "timeout"])
        self.assertEqual(
            self.service.council.read(self.identifier)["attempts"], {"gemini": 1, "claude": 1}
        )
        with self.assertRaises(ValueError):
            self.service.council.reveal(self.identifier)

    async def test_configuration_change_is_rejected_before_reservation(self):
        changed = Service(Settings(state_dir=Path(self.temp.name), workspace=Path(self.temp.name), enabled=True, model="other-model"))
        with self.assertRaisesRegex(ValueError, "configuration changed"):
            await changed.council_round(self.identifier)
        self.assertEqual(self.service.council.read(self.identifier)["model_attempts"], 0)
        await changed.close()

    async def test_cancel_one_model_keeps_other_running(self):
        ready = asyncio.Event()
        release = asyncio.Event()
        async def capture(command, **kwargs):
            if "claude" in command[command.index("--model") + 1]:
                ready.set()
                await asyncio.Event().wait()
            await release.wait()
            return b"", b"", 0, dict(status="SUCCESS", response=envelope("surviving result"))
        with patch("agy_observer.service.shutil.which", return_value="agy"), patch("agy_observer.service.capture", side_effect=capture) as calls:
            task = asyncio.create_task(self.service.council_round(self.identifier))
            await asyncio.wait_for(ready.wait(), 3)
            run_id = next(key for key, model in self.service.active_models.items() if "claude" in model)
            self.service.cancel(run_id)
            release.set()
            result = await asyncio.wait_for(task, 3)
        self.assertEqual([m["state"] for m in result["outcomes"]], ["success", "cancelled"])
        self.assertEqual(calls.call_count, 2)

    async def test_cancel_batch_finishes_both_and_rejects_duplicate(self):
        ready = asyncio.Event()
        calls = 0

        async def capture(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                ready.set()
            await asyncio.Event().wait()

        with (
            patch("agy_observer.service.shutil.which", return_value="agy"),
            patch("agy_observer.service.capture", side_effect=capture),
        ):
            task = asyncio.create_task(self.service.council_round(self.identifier))
            await asyncio.wait_for(ready.wait(), 3)
            with self.assertRaisesRegex(ValueError, "active batch"):
                await self.service.council_round(self.identifier)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        data = self.service.council.read(self.identifier)
        self.assertEqual([m["state"] for m in data["messages"]][1:], ["cancelled", "cancelled"])
        self.assertEqual(data["model_attempts"], 2)
        self.assertEqual(self.service.active, {})

    async def test_disabled_and_recovery_never_invoke_model(self):
        disabled = Service(Settings(state_dir=Path(self.temp.name)))
        with self.assertRaises(ValueError):
            await disabled.council_round(self.identifier)
        self.assertEqual(self.service.council.read(self.identifier)["model_attempts"], 0)
        batch = self.service.council.reserve(self.identifier, ["gemini", "claude"])
        record = RunRecord(self.service.history.root, "synthetic", self.service.settings.model)
        self.service.council.link(
            self.identifier, batch[0][1], record.run_id, self.service.settings.model
        )
        record.finish("success", response=envelope("saved result"))
        result = self.service.discussion_recover(self.identifier)
        self.assertEqual([m["state"] for m in result["messages"]][1:], ["success", "interrupted"])
        self.assertEqual(result, self.service.discussion_recover(self.identifier))
        await disabled.close()

    async def test_recovery_extracts_valid_body_without_repeating_model(self):
        batch = self.service.council.reserve(self.identifier, ["gemini", "claude"])
        for _, mid, _, model in batch:
            record = RunRecord(self.service.history.root, "synthetic", model)
            self.service.council.link(self.identifier, mid, record.run_id, model)
            record.finish("success", response=envelope("readable reply", question="new question?"))
            self.service.council.finish(self.identifier, mid, "success", "legacy broken envelope")
        before = self.service.council.read(self.identifier)
        with patch("agy_observer.service.capture") as capture:
            self.service.discussion_recover(self.identifier)
        after = self.service.council.read(self.identifier)
        self.assertEqual(after["attempts"], before["attempts"])
        for old, new in zip(before["messages"][1:], after["messages"][1:]):
            self.assertEqual(new["content"], "readable reply")
            self.assertEqual(new["finished_utc"], old["finished_utc"])
        capture.assert_not_called()

    async def test_http_never_exposes_sealed_initial_content(self):
        self.service.monitor.start()
        client = http.client.HTTPConnection(
            "127.0.0.1", self.service.monitor.server.server_port, timeout=2
        )
        client.request("GET", "/api/discussions/" + self.identifier)
        response = client.getresponse()
        self.assertEqual(response.status, 200)
        self.assertNotIn("GPT synthetic", response.read().decode())
        client.close()
