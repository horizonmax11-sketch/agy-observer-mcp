import asyncio
import http.client
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from agy_observer.config import Settings
from agy_observer.history import History
from agy_observer.monitor import Monitor
from agy_observer.runtime import CaptureError, RunRecord, capture, strict_json
from agy_observer.service import Service, validate_result


class ConfigTests(unittest.TestCase):
    def test_execution_default_off(self):
        with patch.dict(
            os.environ, {"AGY_OBSERVER_STATE_DIR": str(Path.cwd() / "state")}, clear=True
        ):
            self.assertFalse(Settings.from_env().enabled)

    def test_model_labels_not_flags(self):
        for model in ("--bad", "x\n", ""):
            with self.assertRaises(ValueError):
                Settings(model=model)

    def test_timeout_ceiling(self):
        with self.assertRaises(ValueError):
            Settings(outer_timeout=300)

    def test_environment_does_not_forward_provider_keys(self):
        with patch.dict(
            os.environ,
            {"EXAMPLE_API_KEY": "synthetic-secret", "HOME": "synthetic-home"},
            clear=True,
        ):
            env = Settings().cli_environment()
            self.assertNotIn("EXAMPLE_API_KEY", env)
            self.assertEqual(env["HOME"], "synthetic-home")


class ParserTests(unittest.TestCase):
    def test_strict_json(self):
        for text in ('{"x":1,"x":2}', '{"x":NaN}', '{"x":Infinity}', '{"x":1e999}'):
            with self.subTest(text=text), self.assertRaises(ValueError):
                strict_json(text)

    def test_result_fail_closed(self):
        for value in (
            None,
            {},
            dict(status="ERROR", response="partial"),
            dict(status="SUCCESS", response=""),
            dict(status="SUCCESS", response="ok", usage={"total_tokens": -1}),
        ):
            with self.assertRaises(ValueError):
                validate_result(value)

    def test_valid_result(self):
        self.assertEqual(validate_result(dict(status="SUCCESS", response="ok\n")), "ok")


class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.history = History(self.temp.name)

    def test_round_trip_and_filter(self):
        r = RunRecord(self.history.root, "synthetic prompt", "none", "离线模拟")
        r.finish("success", response="synthetic response", exit_code=0)
        self.assertEqual(self.history.list(), [])
        self.assertEqual(len(self.history.list(include_tests=True)), 1)
        self.assertEqual(self.history.status(r.run_id)["response"], "synthetic response")
        self.assertEqual(self.history.events(r.run_id)[-1]["event"], "terminal")

    def test_traversal(self):
        for run_id in ("../other", "a/b", "a\\b", "", "%2e%2e"):
            with self.assertRaises(ValueError):
                self.history.status(run_id)

    def test_list_limit(self):
        with self.assertRaises(ValueError):
            self.history.list(1000)

    def test_symlink(self):
        self.history.root.mkdir()
        try:
            (self.history.root / "outside").symlink_to(
                Path(self.temp.name), target_is_directory=True
            )
        except OSError:
            self.skipTest("symlink creation unavailable")
        with self.assertRaises(ValueError):
            self.history.status("outside")


class MonitorTests(unittest.TestCase):
    def test_readonly_routes(self):
        with tempfile.TemporaryDirectory() as folder:
            monitor = Monitor(folder)
            monitor.start()
            self.addCleanup(monitor.close)
            port = monitor.server.server_port
            for path, expected in [
                ("/", 200),
                ("/api/runs", 200),
                ("/../config.py", 404),
                ("/%2e%2e/config.py", 404),
                ("/api/events/../x", 404),
            ]:
                conn = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
                conn.request("GET", path)
                response = conn.getresponse()
                response.read()
                self.assertEqual(response.status, expected)
                conn.close()
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
            conn.request("GET", "/", headers={"Host": "external.example"})
            response = conn.getresponse()
            response.read()
            self.assertEqual(response.status, 403)
            conn.close()
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
            conn.request("POST", "/api/runs")
            response = conn.getresponse()
            response.read()
            self.assertEqual(response.status, 501)
            conn.close()


class ServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.settings = Settings(
            state_dir=Path(self.temp.name),
            workspace=Path(self.temp.name),
            enabled=True,
            open_browser=False,
        )
        self.service = Service(self.settings)
        self.addAsyncCleanup(self.service.close)

    async def test_disabled_never_spawns(self):
        service = Service(Settings(state_dir=Path(self.temp.name)))
        with patch("agy_observer.service.capture", new_callable=AsyncMock) as spawn:
            with self.assertRaisesRegex(ValueError, "disabled"):
                await service.run("hello")
            spawn.assert_not_called()

    async def test_browser_opener_keeps_stdio_private(self):
        if os.name == "nt":
            with patch("os.startfile") as opener:
                await self.service.monitor.open()
                opener.assert_called_once_with(self.service.monitor.url)
        else:
            process = AsyncMock()
            process.wait.return_value = 0
            with (
                patch("agy_observer.monitor.Path.is_file", return_value=False),
                patch("agy_observer.monitor.shutil.which", return_value="/usr/bin/xdg-open"),
                patch(
                    "agy_observer.monitor.asyncio.create_subprocess_exec",
                    new_callable=AsyncMock,
                    return_value=process,
                ) as spawn,
            ):
                await self.service.monitor.open()
                self.assertEqual(spawn.call_args.kwargs["stdout"], asyncio.subprocess.DEVNULL)
                self.assertEqual(spawn.call_args.kwargs["stderr"], asyncio.subprocess.DEVNULL)

    async def test_mock_one_call_and_no_bypass(self):
        payload = dict(status="SUCCESS", response="synthetic response", conversation_id="SIMULATED")
        with (
            patch("agy_observer.service.shutil.which", return_value="agy"),
            patch(
                "agy_observer.service.capture",
                new_callable=AsyncMock,
                return_value=(b"", b"", 0, payload),
            ) as spawn,
        ):
            result = await self.service.run("synthetic prompt")
            spawn.assert_awaited_once()
            command = spawn.call_args.args[0]
            self.assertNotIn("--dangerously-skip-permissions", command)
            self.assertNotIn("--disable-slash-commands", command)
            self.assertEqual(result["response"], "synthetic response")
            self.assertEqual(self.service.history.status(result["run_id"])["state"], "success")

    async def test_soft_denial(self):
        with (
            patch("agy_observer.service.shutil.which", return_value="agy"),
            patch(
                "agy_observer.service.capture",
                new_callable=AsyncMock,
                return_value=(b"", b"soft-denied", 0, dict(status="SUCCESS", response="partial")),
            ),
        ):
            with self.assertRaises(ValueError):
                await self.service.run("synthetic")
        self.assertEqual(self.service.history.list()[0]["state"], "failed")

    async def test_browser_failure_before_cli(self):
        service = Service(
            Settings(state_dir=Path(self.temp.name), workspace=Path(self.temp.name), enabled=True)
        )
        self.addAsyncCleanup(service.close)
        with (
            patch("agy_observer.service.shutil.which", return_value="agy"),
            patch.object(
                service.monitor,
                "open",
                new_callable=AsyncMock,
                side_effect=RuntimeError("synthetic failure"),
            ),
            patch("agy_observer.service.capture", new_callable=AsyncMock) as spawn,
        ):
            with self.assertRaises(RuntimeError):
                await service.run("synthetic")
            spawn.assert_not_called()

    async def test_cancel_owned_task(self):
        started = asyncio.Event()

        async def wait(*args, **kwargs):
            started.set()
            await asyncio.Event().wait()

        with (
            patch("agy_observer.service.shutil.which", return_value="agy"),
            patch("agy_observer.service.capture", side_effect=wait),
        ):
            task = asyncio.create_task(self.service.run("synthetic"))
            await started.wait()
            run_id = next(iter(self.service.active))
            self.assertTrue(self.service.cancel(run_id)["cancel_requested"])
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertEqual(self.service.history.status(run_id)["state"], "cancelled")
        self.assertFalse(self.service.cancel(run_id)["cancel_requested"])


class ProcessTests(unittest.IsolatedAsyncioTestCase):
    async def fixture(self, code, **kwargs):
        return await capture(
            [sys.executable, "-u", "-c", code], cwd=None, env=dict(os.environ), **kwargs
        )

    async def test_stream(self):
        lines = [
            {"event": "init"},
            {
                "event": "step_update",
                "step_update": {"step_type": "agent_response", "text_delta": "synthetic"},
            },
            {"event": "result", "result": {"status": "SUCCESS", "response": "synthetic"}},
        ]
        code = ";".join("print(" + repr(json.dumps(row)) + ",flush=True)" for row in lines)
        self.assertEqual(
            (await self.fixture(code, stream=True, timeout=3))[3]["response"], "synthetic"
        )

    async def test_dual_output_caps(self):
        for stream in ("stdout", "stderr"):
            with self.subTest(stream=stream), self.assertRaises(CaptureError):
                await self.fixture(
                    f"import sys;sys.{stream}.write('x'*1000000);sys.{stream}.flush()",
                    limit=8192,
                    timeout=3,
                )

    async def test_invalid_event_order(self):
        with self.assertRaises(CaptureError):
            await self.fixture('print(\'{"event":"result","result":{}}\')', stream=True, timeout=3)

    async def test_timeout_tree(self):
        with tempfile.TemporaryDirectory() as folder:
            marker = Path(folder) / "survived"
            child = f"import time,pathlib;time.sleep(2);pathlib.Path({str(marker)!r}).touch();time.sleep(20)"
            parent = f"import subprocess,sys,time;subprocess.Popen([sys.executable,'-c',{child!r}]);time.sleep(20)"
            with self.assertRaises(asyncio.TimeoutError):
                await self.fixture(parent, timeout=0.5)
            await asyncio.sleep(2.2)
            self.assertFalse(marker.exists())

    async def test_cancel_tree(self):
        with tempfile.TemporaryDirectory() as folder:
            marker = Path(folder) / "survived"
            child = f"import time,pathlib;time.sleep(2);pathlib.Path({str(marker)!r}).touch();time.sleep(20)"
            parent = f"import subprocess,sys,time;subprocess.Popen([sys.executable,'-c',{child!r}]);time.sleep(20)"
            task = asyncio.create_task(self.fixture(parent, timeout=10))
            await asyncio.sleep(0.5)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            await asyncio.sleep(2.2)
            self.assertFalse(marker.exists())


if __name__ == "__main__":
    unittest.main()
