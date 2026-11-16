import contextlib
import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from aiui.agent import runner


class Reporter:
    def log(self, *args, **kwargs):
        pass


class ScriptedToolValidationTests(unittest.IsolatedAsyncioTestCase):
    async def execute(self, steps, capture=False):
        calls = []

        class Session:
            async def list_tools(self):
                return SimpleNamespace(tools=[SimpleNamespace(name="navigate")])

            async def call_tool(self, name, args, **kwargs):
                calls.append(name)
                return SimpleNamespace(is_error=False, content=[SimpleNamespace(
                    type="text", text=json.dumps({"ok": True}))])

        @contextlib.asynccontextmanager
        async def open_session(*args, **kwargs):
            yield Session()

        agent = runner.Agent("http://unused", "test", "127.0.0.1", 0, 1, None)
        self.addCleanup(agent.pool.shutdown)
        task = {"case": {"mode": "scripted", "timeout_s": 15, "steps": steps},
                "mcp": {"id": "builtin-browser", "transport": "http"}}
        if capture:
            task["profile_capture"] = {"profile_id": "P-test"}
        with patch.object(runner.mcp_runtime, "open_session", open_session):
            result = await agent._execute(task, Reporter())
        return result, calls

    async def test_mismatched_tools_fail_without_retry_for_test_and_capture(self):
        for capture in (False, True):
            with self.subTest(capture=capture):
                result, calls = await self.execute([
                    {"tool": "playwright_navigate", "args": {"url": "http://example.test"}}
                ], capture=capture)
                self.assertFalse(result["ok"])
                self.assertIs(result.get("retryable"), False)
                self.assertIn("playwright_navigate", result["error"])
                self.assertIn("builtin-browser", result["error"])
                self.assertEqual(calls, [])

    async def test_later_unknown_tool_is_detected_before_any_browser_action(self):
        result, calls = await self.execute([
            {"tool": "navigate", "args": {"url": "http://example.test"}},
            {"tool": "playwright_fill", "args": {}},
        ])
        self.assertFalse(result["ok"])
        self.assertIn("第 2 步", result["error"])
        self.assertEqual(calls, [])

    async def test_matching_scripted_tools_still_execute(self):
        result, calls = await self.execute([
            {"tool": "navigate", "args": {"url": "http://example.test"}},
        ])
        self.assertTrue(result["ok"])
        self.assertEqual(calls, ["navigate"])
