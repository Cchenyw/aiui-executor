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


class Session:
    async def call_tool(self, name, args, **kwargs):
        payload = {"ok": True, "text": "observed " + "x" * 500}
        return SimpleNamespace(is_error=False, content=[
            SimpleNamespace(type="text", text=json.dumps(payload))])


class JudgeTranscriptTests(unittest.TestCase):
    def test_short_transcript_is_kept_in_full(self):
        steps = [
            {"tool": "browser_navigate", "args": {}, "result": "page"},
            {"tool": "__finish__", "args": {}, "result": "done"},
        ]
        body, actor, truncated, full_len = ai_executor._judge_transcript(
            steps, "passed", "all good", "page text", limit=10000)
        self.assertFalse(truncated)
        self.assertIn("[1] browser_navigate", body)
        self.assertIn("[2] __finish__", body)
        self.assertIn("执行者自评", actor)
        self.assertEqual(full_len, len(body))

    def test_long_transcript_keeps_head_tail_and_finish(self):
        steps = [
            {"tool": "browser_click", "args": {"i": i}, "result": "r" * 300}
            for i in range(1, 21)
        ]
        steps.append({"tool": "__finish__", "args": {}, "result": "final evidence"})
        body, actor, truncated, full_len = ai_executor._judge_transcript(
            steps, "passed", "finished", "final evidence",
            limit=1200, step_chars=100)
        self.assertTrue(truncated)
        self.assertGreater(full_len, len(body))
        self.assertIn("[1]", body)
        self.assertIn("[21]", body)
        self.assertIn("__finish__", body)
        self.assertIn("执行记录已截断", body)
        self.assertIn("verdict=passed", actor)
        self.assertIn("final evidence", actor)

    def test_actor_prompt_warns_about_unique_selectors(self):
        prompt = ai_executor._system_prompt({}, [tool("snapshot")], 5)
        self.assertIn("strict mode", prompt)
        self.assertIn("nth-child", prompt)


class JudgePromptIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_judge_prompt_includes_actor_summary_and_truncation_note(self):
        judge_messages = []

        class Client:
            model, provider = "test-model", "test"

            def __init__(self, cfg):
                self.n = 0

            def chat(self, messages, system, max_tokens, temperature):
                self.n += 1
                if system == ai_executor.JUDGE_SYSTEM:
                    judge_messages.extend(messages)
                    return {"text": json.dumps({
                        "verdict": "inconclusive", "confidence": 0.0,
                        "reason": "not enough", "evidence": "trace"}),
                        "usage": {}}
                if self.n == 1:
                    action = {"action": "tool", "tool": "snapshot", "args": {"n": 1}}
                elif self.n == 2:
                    action = {"action": "tool", "tool": "snapshot", "args": {"n": 2}}
                else:
                    action = {"action": "finish", "verdict": "passed",
                              "reason": "all steps done", "evidence": "final page"}
                return {"text": json.dumps(action), "usage": {}}

        with patch.object(ai_executor, "LLMClient", Client), \
                patch.object(ai_executor, "JUDGE_TRANSCRIPT_LIMIT", 200), \
                patch.object(ai_executor, "JUDGE_STEP_RESULT_CHARS", 100):
            result = await ai_executor.run_ai_task(
                Session(), [tool("snapshot")],
                {"instruction": "do it", "expected": "done", "max_steps": 5},
                Reporter(), 30, config={"configured": True})

        self.assertEqual(result["judge_status"], "ok")
        self.assertTrue(judge_messages)
        content = judge_messages[0]["content"]
        self.assertIn("执行者自评", content)
        self.assertIn("执行记录已截断", content)
        self.assertIn("__finish__", content)
        self.assertIn("all steps done", content)


if __name__ == "__main__":
    unittest.main()
