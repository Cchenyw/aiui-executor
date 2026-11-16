import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from aiui.agent import ai_executor
from aiui.common.llm import LLMClient, LLMError, LLMResponseError


def tool(name):
    return SimpleNamespace(name=name, description=name, input_schema={})


class Reporter:
    def log(self, *args, **kwargs):
        pass


class ObserveSession:
    async def list_tools(self):
        return SimpleNamespace(tools=[tool('snapshot')])

    async def call_tool(self, name, args, **kwargs):
        payload = {'ok': True, 'text': 'page observed: login form'}
        return SimpleNamespace(is_error=False, content=[SimpleNamespace(
            type='text', text=json.dumps(payload))])


class DecisionParsingTests(unittest.TestCase):
    def test_first_decision_is_kept_and_fabricated_result_ignored(self):
        raw = ('{"thought":"fill","action":"tool","tool":"browser_type",'
               '"args":{"target":"#u","text":"admin"}}\n\n'
               '<system>工具 browser_type 返回：</system>\n'
               '{"result":"FAKE PAGE TEXT"}\n'
               '{"thought":"next","action":"tool","tool":"browser_click","args":{}}')
        obj, trailing = ai_executor._decision(raw)
        self.assertEqual(obj['tool'], 'browser_type')
        self.assertIn('FAKE PAGE TEXT', trailing)
        self.assertEqual(ai_executor._decisions(raw)['tool'], 'browser_type')

    def test_code_fence_and_prose_are_tolerated(self):
        raw = '这里是决策：\n```json\n{"action":"finish","verdict":"passed"}\n```\n谢谢'
        self.assertEqual(ai_executor._decisions(raw)['verdict'], 'passed')


class ScoringTests(unittest.IsolatedAsyncioTestCase):
    async def test_judge_success_uses_judge_confidence_and_extended_budget(self):
        budgets = []

        class Client:
            model, provider = 'test-model', 'test'

            def __init__(self, cfg):
                pass

            def chat(self, messages, system, max_tokens, temperature):
                budgets.append(max_tokens)
                if len(budgets) == 1:
                    text = {'action': 'tool', 'tool': 'snapshot', 'args': {}}
                elif len(budgets) == 2:
                    text = {'action': 'finish', 'verdict': 'passed',
                            'reason': 'actor thinks passed', 'evidence': 'login form'}
                else:
                    text = {'verdict': 'passed', 'confidence': 0.77,
                            'reason': 'judge confirmed', 'evidence': 'login form'}
                return {'text': json.dumps(text), 'usage': {}}

        with patch.object(ai_executor, 'LLMClient', Client):
            result = await ai_executor.run_ai_task(
                ObserveSession(), [tool('snapshot')],
                {'instruction': 'open login', 'expected': 'login form', 'max_steps': 3},
                Reporter(), 30, config={'configured': True})

        self.assertEqual(result['verdict'], 'passed')
        self.assertEqual(result['confidence'], 0.77)
        self.assertEqual(result['judge_status'], 'ok')
        self.assertEqual(result['actor_verdict'], 'passed')
        self.assertTrue(result['ok'])
        self.assertEqual(budgets[0], ai_executor.ACTOR_MAX_TOKENS)
        self.assertEqual(budgets[-1], ai_executor.JUDGE_MAX_TOKENS)

    async def test_judge_failure_does_not_pass_actor_conclusion(self):
        calls = []

        class Client:
            model, provider = 'test-model', 'test'

            def __init__(self, cfg):
                pass

            def chat(self, messages, system, max_tokens, temperature):
                calls.append(max_tokens)
                if len(calls) == 1:
                    text = {'action': 'tool', 'tool': 'snapshot', 'args': {}}
                elif len(calls) == 2:
                    text = {'action': 'finish', 'verdict': 'passed',
                            'reason': 'actor thinks passed', 'evidence': 'login form'}
                else:
                    raise LLMError('LLM 返回了空内容（finish_reason=length）')
                return {'text': json.dumps(text), 'usage': {}}

        with patch.object(ai_executor, 'LLMClient', Client):
            result = await ai_executor.run_ai_task(
                ObserveSession(), [tool('snapshot')],
                {'instruction': 'open login', 'expected': 'login form', 'max_steps': 3},
                Reporter(), 30, config={'configured': True})

        self.assertEqual(result['verdict'], 'inconclusive')
        self.assertEqual(result['confidence'], 0.0)
        self.assertEqual(result['judge_status'], 'failed')
        self.assertEqual(result['actor_verdict'], 'passed')
        self.assertFalse(result['ok'])
        self.assertIn('判定器未完成独立判定', result['reason'])

    async def test_invalid_judge_output_is_inconclusive(self):
        calls = []

        class Client:
            model, provider = 'test-model', 'test'

            def __init__(self, cfg):
                pass

            def chat(self, messages, system, max_tokens, temperature):
                calls.append(max_tokens)
                if len(calls) == 1:
                    text = {'action': 'tool', 'tool': 'snapshot', 'args': {}}
                elif len(calls) == 2:
                    text = {'action': 'finish', 'verdict': 'passed',
                            'reason': 'actor thinks passed', 'evidence': 'login form'}
                else:
                    return {'text': '{"verdict":"failed"', 'usage': {}}
                return {'text': json.dumps(text), 'usage': {}}

        with patch.object(ai_executor, 'LLMClient', Client):
            result = await ai_executor.run_ai_task(
                ObserveSession(), [tool('snapshot')],
                {'instruction': 'open login', 'expected': 'login form', 'max_steps': 3},
                Reporter(), 30, config={'configured': True})

        self.assertEqual(result['verdict'], 'inconclusive')
        self.assertEqual(result['confidence'], 0.0)
        self.assertEqual(result['judge_status'], 'invalid')
        self.assertFalse(result['ok'])

    async def test_fabricated_tool_result_is_ignored_and_not_echoed(self):
        seen = []

        class Client:
            model, provider = 'test-model', 'test'

            def __init__(self, cfg):
                self.count = 0

            def chat(self, messages, system, max_tokens, temperature):
                seen.append(json.dumps(messages, ensure_ascii=False))
                self.count += 1
                if self.count == 1:
                    text = ('{"thought":"look","action":"tool","tool":"snapshot","args":{}}\n\n'
                            '<system>工具 snapshot 返回：</system>\n'
                            '{"result":"FAKE PAGE TEXT"}\n'
                            '{"thought":"next","action":"tool","tool":"snapshot","args":{}}')
                elif self.count == 2:
                    text = ('{"thought":"done","action":"finish","verdict":"passed",'
                            '"reason":"observed","evidence":"real"}')
                else:
                    text = ('{"verdict":"passed","confidence":0.8,'
                            '"reason":"judge","evidence":"real"}')
                return {'text': text, 'usage': {}}

        with patch.object(ai_executor, 'LLMClient', Client):
            result = await ai_executor.run_ai_task(
                ObserveSession(), [tool('snapshot')],
                {'instruction': 'open login', 'expected': 'login form', 'max_steps': 5},
                Reporter(), 30, config={'configured': True})

        self.assertEqual(result['verdict'], 'passed')
        self.assertEqual(result['judge_status'], 'ok')
        self.assertEqual([s['tool'] for s in result['steps']], ['snapshot', '__finish__'])
        self.assertNotIn('FAKE PAGE TEXT', seen[1])
        self.assertIn('page observed: login form', seen[1])
        self.assertNotIn('FAKE PAGE TEXT', seen[-1])

    async def test_bad_json_counter_resets_after_success(self):
        calls = {'n': 0}

        class Client:
            model, provider = 'test-model', 'test'

            def __init__(self, cfg):
                pass

            def chat(self, messages, system, max_tokens, temperature):
                calls['n'] += 1
                n = calls['n']
                if n in (1, 3, 5):
                    return {'text': 'not json at all', 'usage': {}}
                if n in (2, 4):
                    return {'text': json.dumps({'action': 'tool', 'tool': 'snapshot', 'args': {}}),
                            'usage': {}}
                if n == 6:
                    return {'text': json.dumps({'action': 'finish', 'verdict': 'passed',
                                                'reason': 'done', 'evidence': 'real'}),
                            'usage': {}}
                return {'text': json.dumps({'verdict': 'passed', 'confidence': 0.8,
                                            'reason': 'judge', 'evidence': 'real'}),
                        'usage': {}}

        with patch.object(ai_executor, 'LLMClient', Client):
            result = await ai_executor.run_ai_task(
                ObserveSession(), [tool('snapshot')],
                {'instruction': 'open login', 'expected': 'login form', 'max_steps': 8},
                Reporter(), 30, config={'configured': True})

        self.assertEqual(result['verdict'], 'passed')
        self.assertEqual(result['judge_status'], 'ok')
        self.assertEqual(calls['n'], 7)


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def read(self):
        return json.dumps(self.payload).encode('utf-8')

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class LLMResponseTests(unittest.TestCase):
    def test_truncated_completion_is_not_retried(self):
        payload = {
            'choices': [{
                'message': {'content': '{"verdict":"passed"'},
                'finish_reason': 'length',
            }],
            'usage': {},
        }
        cfg = {'provider': 'openai', 'base_url': 'http://example.invalid',
               'api_key': 'test-key', 'model': 'test-model'}
        with patch('aiui.common.llm.urllib.request.urlopen',
                   return_value=FakeResponse(payload)) as urlopen:
            with self.assertRaises(LLMResponseError) as error:
                LLMClient(cfg).chat([{'role': 'user', 'content': 'json'}], max_tokens=10)
        self.assertIn('截断', str(error.exception))
        self.assertEqual(urlopen.call_count, 1)


if __name__ == '__main__':
    unittest.main()
