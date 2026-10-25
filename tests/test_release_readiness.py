"""Release regressions. All model execution is mocked; subprocesses only test locks."""

import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from agy_observer.config import Settings
from agy_observer.discussions import Discussions
from agy_observer.service import Service
from agy_observer.runtime import RunRecord


class TimeoutConfigurationTests(unittest.TestCase):
    def test_environment_defaults_and_bounded_slow_model_configuration(self):
        with patch.dict(os.environ, {"AGY_OBSERVER_STATE_DIR": tempfile.gettempdir()}, clear=True):
            self.assertEqual(
                (Settings.from_env().cli_timeout, Settings.from_env().outer_timeout), (30, 40)
            )
        with patch.dict(
            os.environ,
            {"AGY_OBSERVER_CLI_TIMEOUT": "120", "AGY_OBSERVER_STATE_DIR": tempfile.gettempdir()},
            clear=True,
        ):
            settings = Settings.from_env()
            self.assertEqual((settings.cli_timeout, settings.outer_timeout), (120, 130))
        for values in ((181, 200), (120, 119), (180, 211), (True, 40)):
            with self.subTest(values=values), self.assertRaises(ValueError):
                Settings(cli_timeout=values[0], outer_timeout=values[1])


class AdmissionAndRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.service = Service(Settings(state_dir=root, workspace=root, enabled=True))
        self.addAsyncCleanup(self.service.close)

    def task(self):
        return self.service.council.create(
            "synthetic release check",
            "synthetic facts",
            {"gemini": self.service.settings.model, "claude": self.service.settings.claude_model},
        )["discussion_id"]

    async def test_pending_browser_blocks_other_entry_points_before_budget_reservation(self):
        first, second = self.task(), self.task()
        peer = self.service.discussions.create("synthetic", "lead", "peer", 2)["discussion_id"]
        entered, release, captured = asyncio.Event(), asyncio.Event(), []

        async def open_window(**kwargs):
            entered.set()
            await release.wait()

        async def capture(*args, **kwargs):
            captured.append(1)
            if len(captured) == 2:
                release.set()
            await asyncio.wait_for(release.wait(), 2)
            return b"", b"", 0, dict(status="SUCCESS", response='{"message":"synthetic"}')

        with (
            patch.object(self.service.monitor, "open", side_effect=open_window),
            patch("agy_observer.service.shutil.which", return_value="agy"),
            patch("agy_observer.service.capture", side_effect=capture),
        ):
            pending = asyncio.create_task(self.service.council_round(first))
            try:
                await asyncio.wait_for(entered.wait(), 2)
                with self.assertRaises(ValueError):
                    await asyncio.wait_for(self.service.council_round(second), 0.5)
                with self.assertRaises(ValueError):
                    await self.service.run("synthetic competing call")
                with self.assertRaises(ValueError):
                    await self.service.discussion_turn(peer)
                self.assertEqual(self.service.council.read(second)["model_attempts"], 0)
                self.assertEqual(self.service.discussions.read(peer)["model_attempts"], 0)
            finally:
                release.set()
                await asyncio.wait_for(pending, 3)
        self.assertEqual(len(captured), 2)
        self.assertEqual(self.service.active, {})

    async def test_browser_failure_releases_admission_without_spending_budget(self):
        identifier = self.task()
        with (
            patch.object(self.service.monitor, "open", side_effect=OSError("synthetic")),
            patch("agy_observer.service.shutil.which", return_value="agy"),
        ):
            for _ in range(2):
                with self.assertRaises(OSError):
                    await self.service.council_round(identifier)
        self.assertEqual(self.service.council.read(identifier)["model_attempts"], 0)

    async def test_cancel_before_reservation_releases_admission_and_spends_no_attempts(self):
        identifier = self.task()
        entered, release = asyncio.Event(), asyncio.Event()

        async def open_window(**kwargs):
            entered.set()
            await release.wait()

        async def capture(*args, **kwargs):
            return b"", b"", 0, dict(status="SUCCESS", response='{"message":"synthetic"}')

        with (
            patch.object(self.service.monitor, "open", side_effect=open_window),
            patch("agy_observer.service.shutil.which", return_value="agy"),
            patch("agy_observer.service.capture", side_effect=capture) as calls,
        ):
            pending = asyncio.create_task(self.service.council_round(identifier))
            await asyncio.wait_for(entered.wait(), 2)
            pending.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await pending
            self.assertEqual(self.service.council.read(identifier)["model_attempts"], 0)
            calls.assert_not_called()
            release.set()
            result = await self.service.council_round(identifier)
        self.assertTrue(all(item["state"] == "success" for item in result["outcomes"]))
        self.assertEqual(calls.call_count, 2)

    async def test_shutdown_cancels_batch_waiting_for_browser(self):
        entered = asyncio.Event()

        async def open_window(**kwargs):
            entered.set()
            await asyncio.Event().wait()

        identifier = self.task()
        with (
            patch.object(self.service.monitor, "open", side_effect=open_window),
            patch("agy_observer.service.shutil.which", return_value="agy"),
        ):
            pending = asyncio.create_task(self.service.council_round(identifier))
            try:
                await asyncio.wait_for(entered.wait(), 2)
                await self.service.close()
                await asyncio.sleep(0)
                self.assertTrue(pending.done())
            finally:
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)
        self.assertEqual(self.service.council.read(identifier)["model_attempts"], 0)

    async def test_successful_run_recovers_failed_delivery_without_model_retry(self):
        identifier = self.task()
        original_finish = self.service.council.finish
        fail_once = True

        def finish(*args, **kwargs):
            nonlocal fail_once
            if args[2] == "success" and fail_once:
                fail_once = False
                raise OSError("synthetic storage failure")
            return original_finish(*args, **kwargs)

        async def capture(*args, **kwargs):
            return b"", b"", 0, dict(status="SUCCESS", response='{"message":"saved reply"}')

        with (
            patch.object(self.service.monitor, "open"),
            patch("agy_observer.service.shutil.which", return_value="agy"),
            patch("agy_observer.service.capture", side_effect=capture) as calls,
            patch.object(self.service.council, "finish", side_effect=finish),
        ):
            result = await self.service.council_round(identifier, ["claude"])
        before = self.service.council.read(identifier)
        message = before["messages"][-1]
        self.assertEqual(message["state"], "failed")
        raw_path = self.service.history.root / message["run_id"] / "status.json"
        raw_before = raw_path.read_bytes()
        with patch("agy_observer.service.capture") as recovery_calls:
            self.service.discussion_recover(identifier)
            after = self.service.council.read(identifier)
            self.service.discussion_recover(identifier)
            self.assertEqual(after, self.service.council.read(identifier))
        restored = after["messages"][-1]
        self.assertEqual(restored["state"], "success")
        self.assertEqual(restored["content"], "saved reply")
        self.assertEqual(restored["recovery"]["previous_error"], "OSError")
        self.assertEqual(after["attempts"], before["attempts"])
        self.assertEqual(raw_path.read_bytes(), raw_before)
        self.assertEqual(result["outcomes"][0]["run_id"], message["run_id"])
        self.assertEqual(calls.call_count, 1)
        recovery_calls.assert_not_called()

    async def test_peer_delivery_recovery_and_real_model_failure_are_distinguished(self):
        for terminal in ("success", "failed"):
            identifier = self.service.discussions.create("synthetic", "lead", "peer", 2)[
                "discussion_id"
            ]
            mid, _ = self.service.discussions.reserve(identifier)
            record = RunRecord(self.service.history.root, "synthetic", self.service.settings.model)
            record.state.update(discussion_id=identifier, discussion_message_id=mid)
            self.service.discussions.link(
                identifier, mid, record.run_id, self.service.settings.model
            )
            record.finish(terminal, response='{"message":"saved peer reply"}')
            self.service.discussions.finish(identifier, mid, "failed", error="OSError")
            self.service.discussion_recover(identifier)
            restored = self.service.discussions.read(identifier)["messages"][-1]
            self.assertEqual(restored["state"], terminal)
            if terminal == "success":
                self.assertEqual(restored["content"], "saved peer reply")
                self.assertEqual(restored["recovery"]["previous_state"], "failed")
            else:
                self.assertNotIn("recovery", restored)

    async def test_recovery_rejects_mismatched_run_backlink(self):
        identifier = self.task()
        _, mid, _, model = self.service.council.reserve(identifier, ["claude"])[0]
        record = RunRecord(self.service.history.root, "synthetic", model)
        record.state.update(discussion_id="different-task", discussion_message_id=mid)
        record.finish("success", response='{"message":"wrong task reply"}')
        self.service.council.link(identifier, mid, record.run_id, model)
        self.service.council.finish(identifier, mid, "failed", error="OSError")
        with self.assertRaisesRegex(ValueError, "does not match"):
            self.service.discussion_recover(identifier)
        self.assertEqual(self.service.council.read(identifier)["messages"][-1]["state"], "failed")

    async def test_longer_timeout_is_passed_to_both_process_limits(self):
        from dataclasses import replace

        self.service.settings = replace(self.service.settings, cli_timeout=120, outer_timeout=150)

        async def capture(command, **kwargs):
            self.assertEqual(command[command.index("--print-timeout") + 1], "120s")
            self.assertEqual(kwargs["timeout"], 150)
            return b"", b"", 0, dict(status="SUCCESS", response="synthetic")

        with (
            patch.object(self.service.monitor, "open"),
            patch("agy_observer.service.shutil.which", return_value="agy"),
            patch("agy_observer.service.capture", side_effect=capture) as calls,
        ):
            await self.service.run("synthetic")
        self.assertEqual(calls.call_count, 1)


class CrashLockTests(unittest.TestCase):
    def test_incomplete_marker_write_does_not_leave_unknown_lock(self):
        with tempfile.TemporaryDirectory() as folder:
            store = Discussions(folder)
            identifier = store.create("synthetic", "lead", "peer", 2)["discussion_id"]
            with patch(
                "agy_observer.storage.json.dump", side_effect=OSError("synthetic marker failure")
            ):
                with self.assertRaises(OSError):
                    store.note(identifier, "not committed")
            self.assertFalse((store.root / "write.lock").exists())
            self.assertFalse(list(store.root.glob(".writer-*.tmp")))
            store.note(identifier, "next write works")

    def test_crashed_writer_releases_os_lock_and_preserves_recovery_audit(self):
        with tempfile.TemporaryDirectory() as folder:
            store = Discussions(folder)
            identifier = store.create("synthetic", "lead", "peer", 2)["discussion_id"]
            script = (
                "import os,sys;from agy_observer.discussions import Discussions;"
                "s=Discussions(sys.argv[1]);c=s.writing();c.__enter__();os._exit(0)"
            )
            subprocess.run([sys.executable, "-B", "-c", script, folder], check=True, timeout=5)
            self.assertTrue((store.root / "write.lock").exists())
            store.note(identifier, "after crash")
            store.create("after crash", "lead", "peer", 2)
            self.assertFalse((store.root / "write.lock").exists())
            rows = (store.root / "lock-recoveries.jsonl").read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(rows), 1)
            self.assertEqual(json.loads(rows[0])["previous_owner"]["pid"] > 0, True)

    def test_active_writer_rejected_without_removing_marker(self):
        with tempfile.TemporaryDirectory() as folder:
            store = Discussions(folder)
            identifier = store.create("synthetic", "lead", "peer", 2)["discussion_id"]
            other = Discussions(folder)
            with store.writing():
                marker = (store.root / "write.lock").read_bytes()
                with self.assertRaisesRegex(ValueError, "busy"):
                    other.note(identifier, "must not write")
                self.assertEqual((store.root / "write.lock").read_bytes(), marker)
            other.note(identifier, "after release")

    def test_legacy_unknown_lock_is_preserved(self):
        with tempfile.TemporaryDirectory() as folder:
            store = Discussions(folder)
            identifier = store.create("synthetic", "lead", "peer", 2)["discussion_id"]
            marker = store.root / "write.lock"
            marker.write_text("legacy unknown writer", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "busy"):
                store.note(identifier, "must not remove unknown lock")
            self.assertEqual(marker.read_text(encoding="utf-8"), "legacy unknown writer")
