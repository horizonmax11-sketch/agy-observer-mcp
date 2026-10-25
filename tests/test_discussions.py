"""No model calls: exercise collaboration via store, service and read-only HTTP seams."""

import asyncio
import errno
import http.client
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import AsyncMock, patch

from agy_observer.config import Settings
from agy_observer.discussions import Discussions, MAX_BYTES
from agy_observer.monitor import Monitor
from agy_observer.runtime import RunRecord
from agy_observer.service import Service


class DiscussionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Discussions(self.temp.name)
        self.task = self.store.create(
            "synthetic goal", "lead implements and verifies", "reviewer critiques", 2
        )
        self.identifier = self.task["discussion_id"]

    def proposal(self):
        return self.store.note(self.identifier, "synthetic proposal", "proposal")

    def test_transient_replace_denial_retries_same_atomic_write(self):
        import os
        replace = os.replace
        before = self.store.file(self.identifier).read_bytes()
        def occupied(source, target):
            self.assertEqual(self.store.file(self.identifier).read_bytes(), before)
            return replace(source, target)
        with patch("agy_observer.discussions.os.replace") as mocked:
            def retry(source, target):
                if mocked.call_count < 3:
                    raise PermissionError(errno.EACCES, "busy")
                return occupied(source, target)
            mocked.side_effect = retry
            result = self.proposal()
        self.assertEqual(mocked.call_count, 3)
        self.assertEqual(self.store.read(self.identifier), result)
        self.assertFalse(list(self.store.root.glob("*.tmp")))

    def test_persistent_replace_failure_preserves_old_document_and_releases_lock(self):
        before = self.store.file(self.identifier).read_bytes()
        with patch("agy_observer.discussions.os.replace", side_effect=PermissionError(errno.EACCES, "busy")), patch("agy_observer.storage.time.sleep"):
            with self.assertRaises(PermissionError):
                self.proposal()
        self.assertEqual(self.store.file(self.identifier).read_bytes(), before)
        self.assertFalse(list(self.store.root.glob("*.tmp")))
        self.assertFalse((self.store.root / "write.lock").exists())

    def test_reader_observes_only_complete_documents_during_replacement(self):
        stop = threading.Event()
        ready = threading.Event()
        errors = []
        viewing = Discussions(self.temp.name)  # Monitor and service use separate stores.
        def read_continuously():
            ready.set()
            while not stop.is_set():
                try:
                    data = viewing.read(self.identifier)
                    self.assertEqual(data["discussion_id"], self.identifier)
                    self.assertTrue(data["goal"].startswith("synthetic"))
                except BaseException as error:
                    errors.append(error)
                    return
        reader = threading.Thread(target=read_continuously)
        reader.start()
        try:
            self.assertTrue(ready.wait(2))
            for i in range(40):
                with self.store.writing():
                    data = self.store.read(self.identifier)
                    data["goal"] = "synthetic " + str(i)
                    self.store.save(data)
        finally:
            stop.set()
            reader.join(3)
        self.assertFalse(reader.is_alive())
        self.assertEqual(errors, [])

    def test_budget_roles_and_closed_summary(self):
        self.proposal()
        first, _ = self.store.reserve(self.identifier, "review", 1)
        self.store.finish(self.identifier, first, "failed", error="synthetic")
        second, _ = self.store.reserve(self.identifier, "explicit retry", first)
        self.store.finish(self.identifier, second, "success", "synthetic review")
        with self.assertRaisesRegex(ValueError, "budget"):
            self.store.reserve(self.identifier, "over budget")
        final = self.store.note(self.identifier, "done", "summary")
        self.assertTrue(final["closed"])
        with self.assertRaisesRegex(ValueError, "closed"):
            self.store.note(self.identifier, "late feedback")
        self.assertEqual(Discussions(self.temp.name).read(self.identifier), final)

    def test_peer_can_open_and_client_can_answer(self):
        message_id, prompt = self.store.reserve(self.identifier)
        self.assertEqual(message_id, 1)
        self.assertIn("对等协作伙伴", prompt)
        data = self.store.finish(
            self.identifier,
            message_id,
            "success",
            json.dumps(
                dict(
                    message="alternative proposal",
                    question="What evidence do you have?",
                    answers_to=[],
                )
            ),
        )
        self.assertEqual(data["messages"][0]["kind"], "contribution")
        self.assertEqual(data["open_questions"][0]["message_id"], 1)
        data = self.store.note(self.identifier, "measured evidence", "answer", 1)
        self.assertEqual(data["open_questions"], [])
        self.assertEqual(data["messages"][-1]["answers_to"], [1])

    def test_fenced_and_once_escaped_json_keep_answer_provenance(self):
        self.store.note(self.identifier, "question?", "question")
        message_id, _ = self.store.reserve(self.identifier)
        body = json.dumps(
            dict(
                message='synthetic answer with "quotes" and path \\file',
                question="counter?",
                answers_to=[1],
            )
        )
        escaped = json.dumps(body)[1:-1]
        data = self.store.finish(
            self.identifier, message_id, "success", "```json\\n" + escaped + "\\n```"
        )
        self.assertEqual(data["messages"][-1]["format"], "peer-json")
        self.assertEqual(data["messages"][-1]["answers_to"], [1])
        self.assertEqual(data["open_questions"][0]["message_id"], message_id)

    def test_peer_answer_closes_only_explicit_question(self):
        self.store.note(self.identifier, "client question?", "question")
        self.store.note(self.identifier, "another question?", "question")
        message_id, _ = self.store.reserve(self.identifier)
        data = self.store.finish(
            self.identifier,
            message_id,
            "success",
            json.dumps(
                dict(
                    message="answer to first question", question="Counter-question?", answers_to=[1]
                )
            ),
        )
        self.assertEqual([q["message_id"] for q in data["open_questions"]], [2, 3])
        # Merely replying is not recorded as an explicit answer.
        self.store.note(self.identifier, "some feedback", "feedback", 3)
        self.assertEqual(len(self.store.read(self.identifier)["open_questions"]), 2)
        self.store.note(self.identifier, "bounded archive, still unresolved", "summary")
        self.assertEqual(len(self.store.read(self.identifier)["open_questions"]), 2)

    def test_invalid_peer_answers_do_not_invent_resolution(self):
        self.store.note(self.identifier, "open question?", "question")
        message_id, _ = self.store.reserve(self.identifier)
        data = self.store.finish(
            self.identifier,
            message_id,
            "success",
            json.dumps(dict(message="invalid reference", question=None, answers_to=[99])),
        )
        self.assertEqual(len(data["open_questions"]), 1)
        self.assertEqual(data["messages"][-1]["format"], "plain-text")
        self.assertIn("protocol_warning", data["messages"][-1])
        with self.assertRaisesRegex(ValueError, "other participant"):
            self.store.note(self.identifier, "self-answer not peer reply", "answer", 1)

    def test_question_context_is_bounded_without_losing_storage(self):
        for index in range(8):
            self.store.note(self.identifier, "question " + str(index), "question")
        message_id, prompt = self.store.reserve(self.identifier, reply_to=8)
        context = self.store.read(self.identifier)["messages"][message_id - 1]["context"]
        self.assertIn(8, context["question_ids_included"])
        self.assertEqual(len(context["question_ids_included"]), 4)
        self.assertEqual(len(context["question_ids_omitted"]), 4)
        self.assertEqual(len(self.store.read(self.identifier)["open_questions"]), 8)
        self.assertLessEqual(len(prompt.encode("utf-16-le")) // 2, 16000)

    def test_legacy_session_keeps_original_start_requirement(self):
        path = self.store.file(self.identifier)
        data = json.loads(path.read_text(encoding="utf-8"))
        data.pop("protocol")
        path.write_text(json.dumps(data), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "proposal"):
            self.store.reserve(self.identifier)

    def test_decisions_need_reference_disposition_and_reason(self):
        self.proposal()
        for kind, reply_to, resolution, content in [
            ("decision", None, "accepted", "reason"),
            ("decision", 99, "accepted", "reason"),
            ("decision", True, "accepted", "reason"),
            ("decision", 1, "maybe", "reason"),
            ("feedback", 1, "accepted", "reason"),
            ("decision", 1, "accepted", ""),
        ]:
            with (
                self.subTest(reply_to=reply_to, resolution=resolution),
                self.assertRaises(ValueError),
            ):
                self.store.note(self.identifier, content, kind, reply_to, resolution)
        decision = self.store.note(self.identifier, "reason", "decision", 1, "deferred")[
            "messages"
        ][-1]
        self.assertEqual(decision["source"], "client")
        self.assertEqual(decision["resolution"], "deferred")

    def test_pending_blocks_notes_and_second_review(self):
        self.proposal()
        self.store.reserve(self.identifier, "review")
        with self.assertRaisesRegex(ValueError, "pending"):
            self.store.note(self.identifier, "feedback")
        with self.assertRaisesRegex(ValueError, "pending"):
            self.store.reserve(self.identifier, "duplicate")
        self.assertEqual(self.store.read(self.identifier)["model_attempts"], 1)

    def test_context_keeps_reference_and_decisions_and_declares_truncation(self):
        self.store.note(self.identifier, "old reference " * 250, "proposal")
        self.store.note(self.identifier, "decision reason", "decision", 1, "accepted")
        for index in range(10):
            self.store.note(self.identifier, "feedback " + str(index))
        message_id, prompt = self.store.reserve(self.identifier, "respond to old reference", 1)
        context = self.store.read(self.identifier)["messages"][message_id - 1]["context"]
        self.assertIn(1, context["included_ids"])
        self.assertIn(2, context["included_ids"])
        self.assertIn(1, context["truncated_ids"])
        self.assertIn(3, context["omitted_ids"])
        self.assertLessEqual(len(prompt.encode("utf-16-le")) // 2, 16000)

    def test_store_boundaries_and_foreign_lock(self):
        for identifier in ["../x", "a/b", "a\\b", "", None]:
            with self.assertRaises(ValueError):
                self.store.read(identifier)
        target = self.store.root / "broken.json"
        target.write_text("[]")
        self.assertEqual(len(self.store.list()), 1)
        target.write_bytes(b"x" * (MAX_BYTES + 1))
        with self.assertRaises(ValueError):
            self.store.read("broken")
        lock = self.store.root / "write.lock"
        lock.write_text("synthetic other writer")
        with self.assertRaisesRegex(ValueError, "busy"):
            self.store.note(self.identifier, "cannot overwrite another writer")
        self.assertEqual(lock.read_text(), "synthetic other writer")
        lock.unlink()

    def test_summary_cache_invalidates_on_note(self):
        with patch.object(self.store, "read", wraps=self.store.read) as read:
            first = self.store.list()
            self.assertEqual(first, self.store.list())
            self.assertEqual(read.call_count, 1)
        self.proposal()
        with patch.object(self.store, "read", wraps=self.store.read) as read:
            self.assertNotEqual(first[0]["updated_utc"], self.store.list()[0]["updated_utc"])
            self.assertEqual(read.call_count, 1)
        self.assertNotIn("messages", self.store.list()[0])
        self.assertNotIn("goal", self.store.list()[0])

    def test_long_response_is_excerpt_with_link_preserved(self):
        self.proposal()
        message_id, _ = self.store.reserve(self.identifier, "review")
        self.store.link(self.identifier, message_id, "synthetic-run", "synthetic-model")
        data = self.store.finish(self.identifier, message_id, "success", "x" * 9000)
        message = data["messages"][-1]
        self.assertTrue(message["content_truncated"])
        self.assertEqual(len(message["content"]), 8000)
        self.assertEqual(message["run_id"], "synthetic-run")

    def test_http_is_readonly_and_returns_task_without_model(self):
        self.proposal()
        monitor = Monitor(self.temp.name)
        monitor.start()
        self.addCleanup(monitor.close)
        for path in ["/api/discussions", "/api/discussions/" + self.identifier]:
            connection = http.client.HTTPConnection(
                "127.0.0.1", monitor.server.server_port, timeout=2
            )
            connection.request("GET", path)
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            self.assertTrue(json.loads(response.read()))
            connection.close()
        connection = http.client.HTTPConnection("127.0.0.1", monitor.server.server_port, timeout=2)
        connection.request("POST", "/api/discussions")
        response = connection.getresponse()
        self.assertEqual(response.status, 501)
        response.read()
        connection.close()


class CollaborationServiceTests(unittest.IsolatedAsyncioTestCase):
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
        self.identifier = self.service.discussions.create(
            "synthetic goal", "implement", "review", 2
        )["discussion_id"]
        self.service.discussions.note(self.identifier, "synthetic proposal", "proposal")

    async def test_one_review_carries_context_and_links_run(self):
        payload = dict(status="SUCCESS", response="synthetic critique")
        with (
            patch("agy_observer.service.shutil.which", return_value="agy"),
            patch(
                "agy_observer.service.capture",
                new_callable=AsyncMock,
                return_value=(b"", b"", 0, payload),
            ) as spawn,
        ):
            result = await self.service.discussion_turn(self.identifier, "check proposal", 1)
            spawn.assert_awaited_once()
            self.assertIn("synthetic proposal", spawn.call_args.args[0][-1])
        data = self.service.discussions.read(self.identifier)
        self.assertEqual(data["model_attempts"], 1)
        self.assertEqual(data["messages"][-1]["content"], "synthetic critique")
        run = self.service.history.status(result["run_id"])
        self.assertEqual(run["discussion_id"], self.identifier)
        self.assertEqual(run["discussion_message_id"], 2)
        self.assertEqual(data["messages"][-1]["run_id"], result["run_id"])

    async def test_disabled_does_not_reserve_attempt(self):
        service = Service(Settings(state_dir=Path(self.temp.name)))
        await service.close()
        with self.assertRaises(ValueError):
            await service.discussion_turn(self.identifier, "disabled")
        self.assertEqual(service.discussions.read(self.identifier)["model_attempts"], 0)

    async def test_cancel_persists_terminal_without_automatic_retry(self):
        started = asyncio.Event()

        async def wait(*args, **kwargs):
            started.set()
            await asyncio.Event().wait()

        with (
            patch("agy_observer.service.shutil.which", return_value="agy"),
            patch("agy_observer.service.capture", side_effect=wait) as spawn,
        ):
            task = asyncio.create_task(
                self.service.discussion_turn(self.identifier, "synthetic review")
            )
            await asyncio.wait_for(started.wait(), 3)
            run_id = next(iter(self.service.active))
            self.service.cancel(run_id)
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertEqual(spawn.call_count, 1)
        data = self.service.discussions.read(self.identifier)
        self.assertEqual(data["messages"][-1]["state"], "cancelled")
        self.assertEqual(data["model_attempts"], 1)

    async def test_recovery_requires_terminal_run_and_preserves_budget(self):
        message_id, _ = self.service.discussions.reserve(self.identifier, "review")
        record = RunRecord(self.service.history.root, "synthetic", "none")
        self.service.discussions.link(self.identifier, message_id, record.run_id)
        with self.assertRaisesRegex(ValueError, "not terminal"):
            self.service.discussion_recover(self.identifier)
        record.finish("success", response="saved before interruption")
        restored = self.service.discussion_recover(self.identifier)
        self.assertEqual(restored["messages"][-1]["content"], "saved before interruption")
        self.assertEqual(restored["model_attempts"], 1)
        self.assertEqual(self.service.discussion_recover(self.identifier), restored)

    async def test_pre_registration_recovery(self):
        self.service.discussions.reserve(self.identifier, "reserved before restart")
        data = self.service.discussion_recover(self.identifier)
        self.assertEqual(data["messages"][-1]["state"], "interrupted")
        self.assertEqual(data["model_attempts"], 1)
