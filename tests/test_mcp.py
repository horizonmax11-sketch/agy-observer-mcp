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
                        sorted(
                            [
                                "agy_cancel",
                                "agy_history",
                                "agy_info",
                                "agy_run",
                                "agy_status",
                                "agy_discussion_create",
                                "agy_discussion_read",
                                "agy_discussion_note",
                                "agy_discussion_turn",
                                "agy_discussion_recover",
                                "agy_council_create",
                                "agy_council_note",
                                "agy_council_round",
                                "agy_council_reveal",
                            ]
                        ),
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

                    created = await session.call_tool(
                        "agy_discussion_create", {"goal": "synthetic collaboration"}
                    )
                    import json

                    task = json.loads(created.content[0].text)
                    identifier = task["discussion_id"]
                    self.assertFalse((await session.call_tool("agy_discussion_read", {})).isError)
                    note = await session.call_tool(
                        "agy_discussion_note",
                        {
                            "discussion_id": identifier,
                            "content": "synthetic proposal",
                            "kind": "proposal",
                        },
                    )
                    self.assertFalse(note.isError)
                    denied = await session.call_tool(
                        "agy_discussion_turn",
                        {"discussion_id": identifier, "instruction": "synthetic review"},
                    )
                    self.assertTrue(denied.isError)
                    loaded = await session.call_tool(
                        "agy_discussion_read", {"discussion_id": identifier}
                    )
                    self.assertEqual(json.loads(loaded.content[0].text)["model_attempts"], 0)
                    created = await session.call_tool(
                        "agy_council_create", {"goal": "synthetic council"}
                    )
                    identifier = json.loads(created.content[0].text)["discussion_id"]
                    noted = await session.call_tool(
                        "agy_council_note",
                        {
                            "discussion_id": identifier,
                            "kind": "initial",
                            "content": "SEALED SYNTHETIC",
                        },
                    )
                    self.assertNotIn("SEALED SYNTHETIC", json.dumps(noted.model_dump()))
                    denied = await session.call_tool(
                        "agy_council_round", {"discussion_id": identifier}
                    )
                    self.assertTrue(denied.isError)
                    loaded = await session.call_tool(
                        "agy_discussion_read", {"discussion_id": identifier}
                    )
                    self.assertNotIn("SEALED SYNTHETIC", json.dumps(loaded.model_dump()))
                    self.assertEqual(json.loads(loaded.content[0].text)["model_attempts"], 0)
