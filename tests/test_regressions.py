"""Regression coverage for lifecycle cleanup, bounded history and incremental snapshots."""

import asyncio
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from agy_observer.config import Settings
from agy_observer.history import History, MAX_EVENTS_BYTES
from agy_observer.runtime import RunRecord, capture, stop_group
from agy_observer.service import Service


class HistoryRegressionTests(unittest.TestCase):
    def test_summary_cache_and_full_detail(self):
        with tempfile.TemporaryDirectory() as folder:
            history = History(folder)
            record = RunRecord(history.root, "p" * 500, "synthetic")
            record.finish("success", response="private full reply", exit_code=0)
            with patch.object(history, "status", wraps=history.status) as read:
                first = history.list()
                self.assertEqual(first, history.list())
                self.assertEqual(read.call_count, 1)
            self.assertNotIn("response", first[0])
            self.assertNotIn("task", first[0])
            self.assertEqual(len(first[0]["task_preview"]), 120)
            self.assertEqual(history.status(record.run_id)["response"], "private full reply")

    def test_damaged_status_is_skipped(self):
        with tempfile.TemporaryDirectory() as folder:
            history = History(folder)
            for index, payload in enumerate(["[]", "null", '{"run_id":"wrong"}', "x" * 100]):
                target = history.root / ("damaged_" + str(index))
                target.mkdir(parents=True)
                (target / "status.json").write_text(payload)
            self.assertEqual(history.list(), [])

    def test_events_only_complete_bounded_tail(self):
        with tempfile.TemporaryDirectory() as folder:
            history = History(folder)
            record = RunRecord(history.root, "synthetic", "none")
            path = record.path / "events.jsonl"
            path.write_bytes(
                b"x" * (MAX_EVENTS_BYTES + 10) + b"\n" + b'{"event":"latest"}\n{"event":"partial'
            )
            self.assertEqual(history.events(record.run_id), [{"event": "latest"}])

    def test_snapshots_coalesce_but_terminal_is_current(self):
        with (
            tempfile.TemporaryDirectory() as folder,
            patch("agy_observer.runtime.time.monotonic", return_value=10),
        ):
            record = RunRecord(Path(folder), "synthetic", "none")
            for delta in ["a", "b", "c"]:
                record.state["response"] += delta
                record.emit("cli_step_update", text_delta=delta)
            self.assertEqual(json.loads((record.path / "status.json").read_text())["response"], "")
            record.finish("success")
            self.assertEqual(
                json.loads((record.path / "status.json").read_text())["response"], "abc"
            )
            self.assertEqual(len((record.path / "events.jsonl").read_text().splitlines()), 5)


class LifecycleRegressionTests(unittest.IsolatedAsyncioTestCase):
    async def test_closed_service_rejects_calls(self):
        with tempfile.TemporaryDirectory() as folder:
            service = Service(Settings(state_dir=Path(folder), enabled=True))
            await service.close()
            with self.assertRaisesRegex(ValueError, "shutting down"):
                await service.run("synthetic")
            self.assertFalse(service.history.root.exists())

    async def test_logging_failure_still_cleans_spawned_process(self):
        with tempfile.TemporaryDirectory() as folder:
            record = RunRecord(Path(folder), "synthetic", "none")
            original = record.emit

            def emit(event, **data):
                if event == "spawn":
                    raise OSError("synthetic disk failure")
                return original(event, **data)

            with (
                patch.object(record, "emit", side_effect=emit),
                patch("agy_observer.runtime.stop_group", wraps=stop_group) as stop,
            ):
                with self.assertRaisesRegex(OSError, "disk failure"):
                    await capture(
                        [sys.executable, "-c", "import time;time.sleep(20)"],
                        cwd=None,
                        env=dict(os.environ),
                        record=record,
                    )
                stop.assert_awaited_once()
                self.assertIsNotNone(stop.call_args.args[0].returncode)
            self.assertEqual(record.state["state"], "failed")

    async def test_repeated_cancel_cannot_interrupt_cleanup(self):
        with tempfile.TemporaryDirectory() as folder:
            record = RunRecord(Path(folder), "synthetic", "none")
            started, release = asyncio.Event(), asyncio.Event()

            async def slow_stop(process):
                started.set()
                await release.wait()
                await stop_group(process)

            with patch("agy_observer.runtime.stop_group", side_effect=slow_stop):
                task = asyncio.create_task(
                    capture(
                        [sys.executable, "-c", "import time;time.sleep(20)"],
                        cwd=None,
                        env=dict(os.environ),
                        record=record,
                    )
                )
                while record.state["state"] != "running":
                    await asyncio.sleep(0.01)
                task.cancel()
                await asyncio.wait_for(started.wait(), 3)
                task.cancel()
                await asyncio.sleep(0)
                release.set()
                with self.assertRaises(asyncio.CancelledError):
                    await asyncio.wait_for(task, 5)
            self.assertEqual(record.state["state"], "cancelled")
            self.assertIsNotNone(record.state["exit_code"])

    async def test_truncated_utf8_after_terminal_rejected(self):
        raw = b'{"event":"init"}\n{"event":"result","result":{}}\n\xe4'
        with self.assertRaises(UnicodeDecodeError):
            await capture(
                [sys.executable, "-c", "import sys;sys.stdout.buffer.write(" + repr(raw) + ")"],
                cwd=None,
                env=dict(os.environ),
                stream=True,
                timeout=3,
            )
