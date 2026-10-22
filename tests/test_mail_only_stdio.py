"""Exercise real MCP discovery/dispatch without a credential store or network."""

import asyncio
import sys
import tempfile
import unittest
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


class MailOnlyStdioTests(unittest.IsolatedAsyncioTestCase):
    async def test_scoped_stdio_discovery_and_dispatch_never_open_credentials(self):
        with tempfile.TemporaryDirectory() as temporary:
            missing_store = Path(temporary) / "must-not-be-created"
            params = StdioServerParameters(
                command=sys.executable,
                args=["-m", "gmail_mcp.mail_only", "--allow-account", "allowed@example.test"],
                cwd=str(Path(__file__).resolve().parents[1]),
                env={"GMAIL_MCP_CREDENTIALS_DIR": str(missing_store),
                     "GMAIL_MCP_MODE": "read-only", "GMAIL_MCP_ALLOWED_ACCOUNTS": ""},
            )
            async with asyncio.timeout(20):
                async with stdio_client(params) as (reader, writer):
                    async with ClientSession(reader, writer) as session:
                        await session.initialize()
                        tools = (await session.list_tools()).tools
                        names = {tool.name for tool in tools}
                        self.assertIn("get_thread", names)
                        self.assertNotIn("drive_read", names)
                        self.assertNotIn("send_message", names)
                        self.assertNotIn("create_filter", names)
                        for tool in tools:
                            self.assertIn("account", tool.inputSchema["required"], tool.name)
                            self.assertTrue(tool.annotations.readOnlyHint, tool.name)
                        unauthorized = await session.call_tool("get_thread", {
                            "thread_id": "fake", "account": "unapproved@example.test",
                        })
                        self.assertTrue(unauthorized.isError)
                        self.assertIn("allowlist", str(unauthorized.content))
                        hidden = await session.call_tool("drive_read", {
                            "file_id": "fake", "account": "allowed@example.test",
                        })
                        self.assertTrue(hidden.isError)
                        self.assertIn("Unknown tool", str(hidden.content))
            self.assertFalse(missing_store.exists())


if __name__ == "__main__":
    unittest.main()
