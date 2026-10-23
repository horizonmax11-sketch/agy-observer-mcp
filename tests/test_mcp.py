"""Real local STDIO handshake. Model execution remains off, including a negative run call."""

import asyncio
import os
from pathlib import Path
import sys
import tempfile
import unittest

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


class MCPTests(unittest.IsolatedAsyncioTestCase):
    async def test_discovery_and_disabled_gate(self):
        with tempfile.TemporaryDirectory() as folder:
            env = {
                k: v
                for k, v in os.environ.items()
                if k.upper()
                in {
                    "PATH",
                    "SYSTEMROOT",
                    "WINDIR",
                    "COMSPEC",
                    "HOME",
                    "USERPROFILE",
                    "TEMP",
                    "TMP",
                    "TMPDIR",
                }
            }
            env.update(AGY_OBSERVER_ENABLED="0", AGY_OBSERVER_STATE_DIR=folder, PYTHONUTF8="1")
            params = StdioServerParameters(
                command=sys.executable,
                args=["-B", "-m", "agy_observer.server"],
                env=env,
                cwd=str(Path(__file__).resolve().parents[1]),
            )
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    tools = await session.list_tools()
                    self.assertEqual(
                        sorted(t.name for t in tools.tools),
                        ["agy_cancel", "agy_history", "agy_info", "agy_run", "agy_status"],
                    )
                    run = next(t for t in tools.tools if t.name == "agy_run")
                    self.assertFalse(run.annotations.readOnlyHint)
                    self.assertFalse((await session.call_tool("agy_info", {})).isError)
                    self.assertFalse((await session.call_tool("agy_history", {})).isError)
                    result = await session.call_tool(
                        "agy_run", {"prompt": "synthetic disabled-gate check"}
                    )
                    self.assertTrue(result.isError)
                    self.assertFalse((Path(folder) / "runs").exists())
