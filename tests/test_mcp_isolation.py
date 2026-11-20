"""并发任务的 MCP 隔离回归：每任务独立会话，官方 Playwright MCP 强制 --isolated。"""
import asyncio
import contextlib
import unittest
from unittest.mock import patch

from aiui.agent.runner import Agent, ensure_mcp_isolation

OFFICIAL = {
    'id': 'playwright-mcp', 'name': 'Official Playwright MCP', 'transport': 'stdio',
    'command': 'npx', 'args': ['-y', '@playwright/mcp@0.0.85', '--headless'],
}


class _Tools:
    tools = []


class _Session:
    async def list_tools(self):
        return _Tools()


class _Reporter:
    def __init__(self):
        self.lines = []

    def log(self, message, level='info', source='agent'):
        self.lines.append(str(message))


class McpIsolationTests(unittest.TestCase):
    def agent(self):
        agent = Agent('http://127.0.0.1:1', 'a1', '127.0.0.1', 8001, 2, None)
        self.addCleanup(agent.pool.shutdown, True)
        return agent

    def test_official_playwright_mcp_gets_isolated_flag(self):
        out = ensure_mcp_isolation(OFFICIAL)
        self.assertIn('--isolated', out['args'])
        self.assertEqual(out['args'][:-1], OFFICIAL['args'])
        self.assertNotIn('--isolated', OFFICIAL['args'])  # 不修改入参
        again = ensure_mcp_isolation(out)
        self.assertEqual(again['args'].count('--isolated'), 1)

    def test_builtin_and_http_mcp_keep_config(self):
        builtin = {'id': 'builtin-browser', 'transport': 'stdio', 'command': '{python}',
                   'args': ['-m', 'aiui.toolservers.builtin_browser']}
        self.assertIs(ensure_mcp_isolation(builtin), builtin)
        shared = {'id': 'shared', 'transport': 'http', 'url': 'http://127.0.0.1:9000/mcp'}
        self.assertIs(ensure_mcp_isolation(shared), shared)

    def test_concurrent_tasks_use_separate_isolated_sessions(self):
        agent = self.agent()
        entered = []
        release = asyncio.Event()

        @contextlib.asynccontextmanager
        async def fake_session(mcp, here=None, errlog=None):
            entered.append(mcp)
            if len(entered) >= 2:
                release.set()
            # 两个任务必须同时进入各自的会话；串行执行会在 3 秒后超时失败。
            await asyncio.wait_for(release.wait(), 3)
            yield _Session()

        def task(task_id):
            return {'task_id': task_id,
                    'case': {'name': task_id, 'steps': [], 'timeout_s': 5},
                    'mcp': dict(OFFICIAL)}

        async def run_both():
            return await asyncio.gather(
                agent._execute(task('t1'), _Reporter()),
                agent._execute(task('t2'), _Reporter()))

        with patch('aiui.agent.runner.mcp_runtime.open_session', fake_session):
            results = asyncio.run(run_both())

        self.assertEqual([r.get('ok') for r in results], [True, True])
        self.assertEqual(len(entered), 2)
        self.assertIsNot(entered[0], entered[1])
        for config in entered:
            self.assertIn('--isolated', config['args'])


if __name__ == '__main__':
    unittest.main()
