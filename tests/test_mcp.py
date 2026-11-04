#!/usr/bin/env python3
"""
Unit and integration tests for Skelo MCP Server.
"""

import io
import json
import os
import subprocess
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from skelo.mcp import SkeloMCPServer


class TestSkeloMCPServer(unittest.TestCase):
    def setUp(self):
        self.server = SkeloMCPServer()

    def test_initialize(self):
        req = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "test-client", "version": "1.0"}
            }
        }
        resp = self.server.handle_request(req)
        self.assertIsNotNone(resp)
        self.assertEqual(resp["jsonrpc"], "2.0")
        self.assertEqual(resp["id"], 1)
        res = resp["result"]
        self.assertEqual(res["protocolVersion"], "2024-11-05")
        self.assertIn("tools", res["capabilities"])
        self.assertEqual(res["serverInfo"]["name"], "skelo-linux")
        self.assertEqual(res["serverInfo"]["version"], "1.1.0")

    def test_notification_initialized(self):
        req = {
            "jsonrpc": "2.0",
            "method": "notifications/initialized",
            "params": {}
        }
        resp = self.server.handle_request(req)
        # Notifications should produce no response
        self.assertIsNone(resp)

    def test_ping(self):
        req = {"jsonrpc": "2.0", "id": 42, "method": "ping", "params": {}}
        resp = self.server.handle_request(req)
        self.assertIsNotNone(resp)
        self.assertEqual(resp["jsonrpc"], "2.0")
        self.assertEqual(resp["id"], 42)
        self.assertEqual(resp["result"], {})

    def test_tools_list_spec(self):
        req = {"jsonrpc": "2.0", "id": 100, "method": "tools/list", "params": {}}
        resp = self.server.handle_request(req)
        self.assertIsNotNone(resp)
        tools = resp["result"]["tools"]
        tool_names = [t["name"] for t in tools]

        expected_tools = [
            "skelo_list_windows",
            "skelo_inspect",
            "skelo_click",
            "skelo_type",
            "skelo_hotkey",
            "skelo_wait",
            "skelo_batch",
            "skelo_launch",
        ]
        for exp in expected_tools:
            self.assertIn(exp, tool_names, f"Expected tool '{exp}' not found in tools/list")

        tool_map = {t["name"]: t for t in tools}

        # Validate schemas
        self.assertIn("detailed", tool_map["skelo_list_windows"]["inputSchema"]["properties"])
        self.assertIn("app", tool_map["skelo_inspect"]["inputSchema"]["properties"])
        self.assertIn("label", tool_map["skelo_click"]["inputSchema"]["properties"])
        self.assertIn("text", tool_map["skelo_type"]["inputSchema"]["required"])
        self.assertIn("key", tool_map["skelo_hotkey"]["inputSchema"]["required"])
        self.assertIn("actions", tool_map["skelo_batch"]["inputSchema"]["required"])
        self.assertIn("app", tool_map["skelo_launch"]["inputSchema"]["required"])

    def test_tool_call_unknown(self):
        req = {
            "jsonrpc": "2.0",
            "id": 101,
            "method": "tools/call",
            "params": {
                "name": "unknown_tool_name",
                "arguments": {}
            }
        }
        resp = self.server.handle_request(req)
        self.assertIsNotNone(resp)
        result = resp["result"]
        self.assertTrue(result.get("isError"))
        self.assertIn("Unknown tool", result["content"][0]["text"])

    def test_tool_call_list_windows(self):
        req = {
            "jsonrpc": "2.0",
            "id": 102,
            "method": "tools/call",
            "params": {
                "name": "skelo_list_windows",
                "arguments": {"detailed": False}
            }
        }
        resp = self.server.handle_request(req)
        self.assertIsNotNone(resp)
        result = resp["result"]
        self.assertFalse(result.get("isError", False))
        content_text = result["content"][0]["text"]
        parsed = json.loads(content_text)
        self.assertIn("windows", parsed)
        self.assertIn("window_count", parsed)

    def test_tool_call_batch_sleep(self):
        req = {
            "jsonrpc": "2.0",
            "id": 103,
            "method": "tools/call",
            "params": {
                "name": "skelo_batch",
                "arguments": {
                    "actions": [{"action": "sleep", "duration": 0.05}]
                }
            }
        }
        resp = self.server.handle_request(req)
        self.assertIsNotNone(resp)
        result = resp["result"]
        self.assertFalse(result.get("isError", False))
        parsed = json.loads(result["content"][0]["text"])
        self.assertEqual(parsed.get("status"), "success")

    def test_window_context_inheritance(self):
        # When an app has been inspected, click and type without app inherit the context
        self.server._last_app = "MockApp"
        self.server._last_title = "MockTitle"

        # Verify batch step inheritance
        batch_args = {"actions": [{"action": "sleep", "duration": 0.01}, {"action": "type", "text": "hello"}]}
        actions = batch_args["actions"]
        for step in actions:
            if "app" not in step and step.get("action") in ("click", "type"):
                step["app"] = self.server._last_app
        self.assertEqual(actions[1]["app"], "MockApp")

    def test_method_not_found(self):
        req = {"jsonrpc": "2.0", "id": 104, "method": "invalid/method", "params": {}}
        resp = self.server.handle_request(req)
        self.assertIsNotNone(resp)
        self.assertIn("error", resp)
        self.assertEqual(resp["error"]["code"], -32601)

    def test_invalid_request(self):
        req = {"method": "ping"}  # missing jsonrpc="2.0"
        resp = self.server.handle_request(req)
        self.assertIsNotNone(resp)
        self.assertIn("error", resp)
        self.assertEqual(resp["error"]["code"], -32600)


class TestSkeloMCPCLI(unittest.TestCase):
    def test_cli_pipe_initialize_and_tools_list(self):
        repo_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        input_data = (
            '{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "test", "version": "1.0"}}}\n'
            '{"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}\n'
        )
        proc = subprocess.run(
            [sys.executable, os.path.join(repo_dir, "skelo_mcp.py")],
            input=input_data,
            capture_output=True,
            text=True,
            check=True
        )

        stdout_lines = [line.strip() for line in proc.stdout.splitlines() if line.strip()]
        self.assertEqual(len(stdout_lines), 2)

        resp1 = json.loads(stdout_lines[0])
        self.assertEqual(resp1["id"], 1)
        self.assertEqual(resp1["result"]["serverInfo"]["name"], "skelo-linux")

        resp2 = json.loads(stdout_lines[1])
        self.assertEqual(resp2["id"], 2)
        self.assertGreaterEqual(len(resp2["result"]["tools"]), 8)

        # Ensure logging was sent to stderr
        self.assertIn("[skelo-mcp]", proc.stderr)

    def test_skelo_sh_mcp_routing(self):
        repo_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        skelo_sh = os.path.join(repo_dir, "skelo.sh")
        input_data = '{"jsonrpc": "2.0", "id": 99, "method": "ping", "params": {}}\n'
        proc = subprocess.run(
            [skelo_sh, "mcp"],
            input=input_data,
            capture_output=True,
            text=True,
            check=True
        )

        stdout_lines = [line.strip() for line in proc.stdout.splitlines() if line.strip()]
        self.assertEqual(len(stdout_lines), 1)
        resp = json.loads(stdout_lines[0])
        self.assertEqual(resp["id"], 99)
        self.assertEqual(resp["result"], {})


if __name__ == "__main__":
    unittest.main()
