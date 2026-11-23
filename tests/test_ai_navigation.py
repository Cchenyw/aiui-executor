import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from aiui.agent import ai_executor


def tool(name):
    return SimpleNamespace(name=name, description=name, input_schema={})


class Reporter:
    def log(self, *args, **kwargs):
        pass


def session_result(text):
    return SimpleNamespace(is_error=False, content=[SimpleNamespace(type="text", text=text)])


class EvalSession:
    def __init__(self, result):
        self.result = result
        self.calls = []

    async def call_tool(self, name, args, **kwargs):
        self.calls.append((name, args))
        return session_result(self.result)


class NavigationPromptTests(unittest.IsolatedAsyncioTestCase):
    async def test_navigation_hint_is_injected_before_instruction_and_judge_sees_it(self):
        seen = []

        class Client:
            model, provider = "test-model", "test"

            def __init__(self, cfg):
                self.count = 0

            def chat(self, messages, system, max_tokens, temperature):
                seen.append((system, json.dumps(messages, ensure_ascii=False)))
                self.count += 1
                if self.count == 1:
                    text = {"action": "tool", "tool": "snapshot", "args": {}}
                elif self.count == 2:
                    text = {"action": "finish", "verdict": "passed",
                            "reason": "menu path observed", "evidence": "breadcrumb"}
                else:
                    text = {"verdict": "passed", "confidence": 0.8,
                            "reason": "ok", "evidence": "breadcrumb"}
                return {"text": json.dumps(text), "usage": {}}

        hint = ("柜台委托 > 盘后固定价格交易\n"
                "预期面包屑：柜台委托 / 盘后固定价格交易")
        with patch.object(ai_executor, "LLMClient", Client):
            result = await ai_executor.run_ai_task(
                EvalSession(json.dumps({"ok": True, "text": "breadcrumb"})),
                [tool("snapshot")],
                {"instruction": "点击菜单进入页面", "expected": "看到面包屑",
                 "max_steps": 3, "navigation_hint": hint,
                 "name": "nav"},
                Reporter(), 30, config={"configured": True})
        self.assertTrue(result["ok"])
        self.assertTrue(result["navigation_hint_used"])
        system, messages = seen[0]
        self.assertIn("必须像真实用户一样操作", system)
        self.assertIn("不要用 navigate", system)
        content = json.loads(messages)[0]["content"]
        self.assertIn(hint, content)
        self.assertLess(content.find(hint), content.find("自然语言步骤/目标"))
        judge_content = json.loads(seen[-1][1])[0]["content"]
        self.assertIn(hint, judge_content)  # 判定器也看到了导航提示

    async def test_inspect_page_virtual_tool_is_catalogued_and_handled(self):
        payload = {
            "url": "http://example.test/home", "title": "首页",
            "breadcrumb": ["柜台委托", "盘后固定价格交易"],
            "menus": ["柜台委托"],
            "forms": [{"label": "委托价格：", "value": "330.0000",
                       "selector": '.af-form-item:has-text("委托价格：") input'}],
            "tables": [{"headers": ["价格上限", "价格下限"],
                        "rows": [["330.0000", "270.0000"]]}],
            "messages": [],
        }
        session = EvalSession("### Result\n" + json.dumps(payload, ensure_ascii=False)
                              + "\n### Ran Playwright code")
        seen = []

        class Client:
            model, provider = "test-model", "test"

            def __init__(self, cfg):
                self.count = 0

            def chat(self, messages, system, max_tokens, temperature):
                seen.append(json.dumps(messages, ensure_ascii=False))
                self.count += 1
                if self.count == 1:
                    text = {"action": "tool", "tool": "inspect_page",
                            "args": {"include_tables": True}}
                elif self.count == 2:
                    text = {"action": "finish", "verdict": "passed",
                            "reason": "arrived", "evidence": "面包屑"}
                else:
                    text = {"verdict": "passed", "confidence": 0.8,
                            "reason": "ok", "evidence": "面包屑"}
                return {"text": json.dumps(text), "usage": {}}

        with patch.object(ai_executor, "LLMClient", Client):
            result = await ai_executor.run_ai_task(
                session, [tool("browser_evaluate")],
                {"instruction": "观察页面", "expected": "看到面包屑", "max_steps": 3},
                Reporter(), 30, config={"configured": True})
        self.assertTrue(result["ok"])
        self.assertEqual(result["steps"][0]["tool"], "inspect_page")
        self.assertIn("盘后固定价格交易", result["steps"][0]["result"])
        self.assertIn("selector", result["steps"][0]["result"])
        self.assertEqual(session.calls[0][0], "browser_evaluate")
        self.assertIn("function", session.calls[0][1])
        judge_content = json.loads(seen[-1])[0]["content"]
        self.assertIn("关键页面状态", judge_content)
        self.assertIn("330.0000", judge_content)
        self.assertIn("委托价格", judge_content)


class VirtualToolTests(unittest.IsolatedAsyncioTestCase):
    async def test_inspect_page_compacts_snapshot_fallback(self):
        body = json.dumps({"ok": True, "url": "http://x", "title": "T",
                           "text": "hello", "elements": [
                               {"tag": "input", "label": "账号", "selector": "#u",
                                "text": "", "placeholder": "账号"}]*50})
        text, is_error = await ai_executor._inspect_page(
            EvalSession(body), [tool("snapshot")], {}, 10)
        self.assertFalse(is_error)
        obj = json.loads(text)
        self.assertEqual(obj["url"], "http://x")
        self.assertLessEqual(len(obj["elements"]), 40)

    async def test_expect_visible_text_uses_evaluate_when_no_native_tool(self):
        session = EvalSession('### Result\n{"ok": true, "visible": true, "text": "登录"}\n')
        text, is_error = await ai_executor._expect_visible_text(
            session, [tool("browser_evaluate")], {"text": "登录", "timeout_ms": 1000}, 10)
        self.assertFalse(is_error)
        self.assertTrue(json.loads(text)["visible"])
        self.assertEqual(session.calls[0][0], "browser_evaluate")


class ObservationCompactionTests(unittest.TestCase):
    def memory(self):
        memory = {"navigation": [], "pages": [], "facts": {}, "errors": [],
                  "committed": False}
        ai_executor._update_task_memory(
            memory, "browser_click",
            {"element": "顶部菜单 交易管理"}, "", False, 4)
        ai_executor._update_task_memory(
            memory, "inspect_page", {},
            json.dumps({
                "url": "http://example.test/reference",
                "title": "参考页",
                "breadcrumb": ["交易管理", "交易参考信息管理"],
                "forms": [{"label": "证券代码：", "value": "510050",
                           "selector": 'input[placeholder="请输入"]'}],
                "tables": [{"headers": ["价格上限", "价格下限"],
                            "rows": [["330.0000", "270.0000"]]}],
                "text": "盘后固定价格交易",
            }, ensure_ascii=False), False, 14)
        return memory

    def test_task_memory_extracts_navigation_pages_and_facts(self):
        memory = self.memory()
        self.assertEqual(memory["navigation"][0]["target"], "顶部菜单 交易管理")
        self.assertEqual(memory["pages"][0]["breadcrumb"],
                         ["交易管理", "交易参考信息管理"])
        self.assertEqual(memory["facts"]["证券代码："], "510050")
        block = ai_executor._format_task_memory(memory)
        self.assertIn("【任务记忆】", block)
        self.assertIn("330.0000", block)
        self.assertIn("510050", block)

    def test_compact_context_removes_old_observations_and_inserts_memory(self):
        messages = [{"role": "user", "content": "初始目标：点击菜单"}]
        for i in range(8):
            messages.append({"role": "assistant",
                             "content": json.dumps({"action": "tool",
                                                    "tool": "tool%d" % i})})
            messages.append({"role": "user",
                             "content": "工具 tool%d 返回：\n" % i + ("x" * 2000)})
        memory = self.memory()
        removed = ai_executor._compact_context(messages, memory, keep=3)
        self.assertEqual(removed, 5)
        self.assertEqual(messages[0]["content"], "初始目标：点击菜单")
        self.assertTrue(any(str(m.get("content") or "").startswith("【任务记忆】")
                            for m in messages))
        recent = [m for m in messages if str(m.get("content") or "").startswith("工具 ")]
        self.assertEqual(len(recent), 3)
        self.assertIn("tool5 返回", recent[0]["content"])
        self.assertTrue(all(len(m["content"]) > 100 for m in recent))

    def test_token_budget_triggers_compaction_without_char_window(self):
        messages = [{"role": "user", "content": "短消息"}]
        self.assertTrue(ai_executor._should_compact(
            ai_executor.ACTOR_CONTEXT_TOKEN_BUDGET, messages, ""))
        self.assertFalse(ai_executor._should_compact(
            ai_executor.ACTOR_CONTEXT_TOKEN_BUDGET - 1, messages, ""))
        self.assertFalse(ai_executor._should_compact(0, messages, ""))


class AuditTests(unittest.TestCase):
    def test_audit_flags_direct_navigation_and_js_writes_without_blocking(self):
        state = {}
        self.assertEqual(ai_executor._audit_tool_use(
            "browser_navigate", {"url": "http://x"}, state), [])
        warnings = ai_executor._audit_tool_use(
            "browser_navigate", {"url": "http://y"}, state)
        self.assertTrue(any("直接 URL 跳转" in w for w in warnings))
        warnings = ai_executor._audit_tool_use(
            "browser_evaluate", {"function": "input.value = 'x'"}, state)
        self.assertTrue(any("直接改值" in w for w in warnings))
        self.assertEqual(ai_executor._audit_tool_use(
            "browser_evaluate", {"function": "document.body.innerText"}, state), [])
        warnings = ai_executor._audit_tool_use(
            "browser_snapshot", {}, {"inspect_available": True})
        self.assertTrue(any("整页快照" in w for w in warnings))

    def test_judge_transcript_includes_user_level_warnings(self):
        steps = [{"tool": "browser_navigate", "args": {"url": "http://x"},
                  "result": "ok", "user_level_warnings": ["直接 URL 跳转（非用户级操作）"]}]
        transcript, _, _, _ = ai_executor._judge_transcript(steps, "", "", "")
        self.assertIn("用户级操作告警", transcript)
        self.assertIn("直接 URL 跳转", transcript)


if __name__ == "__main__":
    unittest.main()
