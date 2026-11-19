"""远程节点的持久化、协议及回环网络回归。"""
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from aiui.agent.remote import GatewayClient, GatewayError, RemoteWorker, load_config, main
from aiui.agent.node_state import NodeState


class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.config = dict(platform_url='http://127.0.0.1:1', node_id='n1', machine_id='m1',
                           name='Node', mcp_ids=['builtin-browser'], state_dir=str(self.root / 'node'),
                           token_env='TEST_REMOTE_TOKEN')
        self.env = patch.dict(os.environ, TEST_REMOTE_TOKEN='secret')
        self.env.start(); self.addCleanup(self.env.stop)

    def task(self, state, dispatch='d1'):
        return dict(task_id='t1', dispatch_id=dispatch, instance_id=state.instance_id,
                    case={'steps': []}, mcp={'id': 'builtin-browser'})

    def worker(self):
        w = RemoteWorker(self.config)
        self.addCleanup(w.close)
        return w

    def test_state_deduplicates_persists_and_locks_directory(self):
        s = NodeState(self.config['state_dir'])
        task = self.task(s)
        self.assertTrue(s.accept(task)); self.assertFalse(s.accept(task))
        with self.assertRaises(RuntimeError):
            NodeState(self.config['state_dir'])
        instance = s.instance_id
        s.close()
        s = NodeState(self.config['state_dir'])
        self.addCleanup(s.close)
        self.assertEqual(s.instance_id, instance)
        self.assertEqual(len(s.queued()), 1)
        with self.assertRaises(ValueError):
            s.accept(dict(task, instance_id='another'))

    def test_running_restart_reports_unknown_without_replay(self):
        s = NodeState(self.config['state_dir']); task = self.task(s)
        s.accept(task); s.mark_running('d1'); s.close()
        w = self.worker()
        self.assertEqual(w.state.queued(), [])
        events = w.state.events()
        self.assertEqual(len(events), 1)
        self.assertFalse(events[0]['payload']['retryable'])
        self.assertIn('状态未知', events[0]['payload']['error'])

    def test_queued_and_results_survive_worker_restart(self):
        s = NodeState(self.config['state_dir']); s.accept(self.task(s)); s.close()
        w = self.worker()
        async def execute(agent, task, reporter):
            return {'ok': True, 'final_text': 'durable'}
        with patch('aiui.agent.remote.Agent._execute', execute):
            w._start_queued(); w.agent.pool.shutdown(wait=True)
        event = w.state.events()[0]; instance = w.state.instance_id
        w.close()
        w = self.worker()
        self.assertEqual(w.state.events()[0], event)
        self.assertEqual(w.state.instance_id, instance)
        self.assertEqual(w.state.queued(), [])

    def test_directory_cannot_change_node_identity(self):
        w = self.worker(); w.close()
        with self.assertRaises(ValueError):
            RemoteWorker(dict(self.config, node_id='other'))

    def test_network_polling_does_not_wait_for_browser(self):
        w = self.worker(); task = self.task(w.state)
        entered = threading.Event(); release = threading.Event()
        async def execute(agent, task, reporter):
            entered.set(); release.wait(5)
            return {'ok': True}
        calls = []
        def request(path, body):
            calls.append((path, body))
            return {'ok': True, 'task': task} if path == '/v1/claim' else {'ok': True}
        try:
            with patch('aiui.agent.remote.Agent._execute', execute), patch.object(w.client, 'request', side_effect=request):
                self.assertTrue(w.step()); self.assertTrue(entered.wait(2))
                w._heartbeat_at = 0
                self.assertTrue(w.step())
                heartbeats = [body for path, body in calls if path == '/v1/heartbeat']
                self.assertEqual(heartbeats[-1]['active_dispatch_ids'], ['d1'])
                registration = calls[0][1]
                self.assertEqual(registration['version'], 1)
                self.assertEqual(registration['machine_id'], 'm1')
                self.assertEqual(registration['features'], ['env_profile'])
        finally:
            release.set()
            w.agent.pool.shutdown(wait=True)

    def test_registration_reports_runtime_and_probe_capabilities(self):
        w = self.worker()
        calls = []

        def request(path, body):
            calls.append((path, body))
            if path == '/v1/register':
                return {'ok': True, 'slots': 1, 'mcps': [{
                    'id': 'builtin-browser', 'command': '{python}',
                    'args': ['-m', 'aiui.toolservers.builtin_browser']}]}
            if path == '/v1/claim':
                return {'ok': True, 'task': None}
            return {'ok': True}

        class FakeProbeProcess:
            def __init__(self, cmd, **kwargs):
                self.returncode = 0
                self.pid = 1234
                output = Path(cmd[cmd.index('--output') + 1])
                output.write_text(json.dumps([{
                    'id': 'builtin-browser', 'available': True, 'tool_count': 1,
                    'tools': ['navigate'], 'profile_capable': True,
                }]), encoding='utf-8')

            def communicate(self, timeout=None):
                return b'', b''

        with patch.object(w.client, 'request', side_effect=request), \
                patch('aiui.agent.remote.subprocess.Popen', new=FakeProbeProcess):
            self.assertTrue(w.step())
            registration = next(body for path, body in calls if path == '/v1/register')
            self.assertEqual(registration['machine_id'], 'm1')
            self.assertEqual(registration['runtime']['protocol'], 1)
            self.assertTrue(registration['runtime']['os'])
            self.assertIn('slots_recommended', registration)
            deadline = time.time() + 5
            while time.time() < deadline and not w._mcp_capabilities:
                time.sleep(0.05)
            self.assertTrue(w._mcp_capabilities)
            w._heartbeat_at = 0
            self.assertTrue(w.step())
            heartbeat = next(body for path, body in calls
                             if path == '/v1/heartbeat' and body.get('mcp_capabilities'))
            capability = heartbeat['mcp_capabilities'][0]
            self.assertEqual(capability['id'], 'builtin-browser')
            self.assertEqual(capability['tool_count'], 1)
            self.assertEqual(capability['tools'], ['navigate'])

    def test_https_uses_validating_context(self):
        import ssl
        original = ssl.create_default_context
        contexts = []
        def context(*args, **kwargs):
            ctx = original(*args, **kwargs); contexts.append(ctx); return ctx
        with patch('aiui.agent.remote.ssl.create_default_context', side_effect=context):
            GatewayClient('https://example.com', 'n1', 'secret')
        self.assertEqual(contexts[0].verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(contexts[0].check_hostname)

    def test_duplicate_dispatch_executes_once_and_logs_precede_result(self):
        w = self.worker(); task = self.task(w.state)
        executions = []
        claims = []
        async def execute(agent, task, reporter):
            executions.append(task['dispatch_id'])
            reporter.log('hello')
            return {'ok': True, 'steps': []}
        def replies(path, body):
            if path == '/v1/claim':
                claims.append(task['dispatch_id'])
                return {'ok': True, 'task': task}
            return {'ok': True, 'status': 'received'}
        with patch.object(w.client, 'request', side_effect=replies), patch('aiui.agent.remote.Agent._execute', execute):
            w.step(); w.agent.pool.shutdown(wait=True)
            events = w.state.events()
            w.step()
        self.assertEqual([e['type'] for e in events], ['logs', 'result'])
        self.assertEqual(claims, ['d1', 'd1'])
        self.assertEqual(executions, ['d1'])
        self.assertEqual(w.state.status()['dispatches'], {'completed': 1})

    def test_backlog_upload_keeps_heartbeat_within_offline_window(self):
        w = self.worker(); w.state.accept(self.task(w.state))
        for i in range(30):
            w.state.logs('d1', [{'ts': i, 'level': 'info', 'source': 'agent', 'message': str(i)}])
        w.state.finish('d1', {'ok': True})
        clock = [0.0]; heartbeats = []; delivered = []
        def request(path, body):
            if path == '/v1/events':
                clock[0] += 0.5
                delivered.append(body['event_id'])
                return {'ok': True, 'status': 'received'}
            if path == '/v1/heartbeat':
                heartbeats.append(clock[0])
            return {'ok': True, 'task': None}
        with patch('aiui.agent.remote.time.monotonic', side_effect=lambda: clock[0]), patch.object(
                w.client, 'request', side_effect=request):
            while w.state.events():
                self.assertTrue(w.step())
                clock[0] += 0.25
        gaps = [b - a for a, b in zip([0.0] + heartbeats, heartbeats + [clock[0]])]
        self.assertLess(max(gaps), 10)
        self.assertEqual(len(delivered), 31)

    def test_failed_upload_does_not_block_identity_maintenance(self):
        for failure in ('413', 'bad_ack'):
            for registered in (True, False):
                with self.subTest(failure=failure, registered=registered):
                    self.config['state_dir'] = str(self.root / f'{failure}-{registered}')
                    w = self.worker()
                    w.state.accept(self.task(w.state))
                    w.state.finish('d1', {'ok': True})
                    event = w.state.events()[0]
                    if not registered:
                        w.close()
                        w = self.worker()  # persisted event after restart
                    w._registered = registered
                    calls = []
                    clock = [0.0]
                    def request(path, body):
                        calls.append((path, clock[0]))
                        if path == '/v1/events':
                            if failure == '413':
                                raise GatewayError('HTTP 413')
                            return {'ok': True}
                        return {'ok': True, 'slots': 1, 'task': None}
                    with patch('aiui.agent.remote.time.monotonic', side_effect=lambda: clock[0]), patch.object(
                            w.client, 'request', side_effect=request):
                        for now in (0, 3, 6, 10, 15, 20):
                            clock[0] = now
                            self.assertFalse(w.step())
                    self.assertEqual([t for path, t in calls if path == '/v1/heartbeat'], [0, 3, 6, 10, 15, 20])
                    self.assertEqual(sum(path == '/v1/register' for path, _ in calls), 0 if registered else 1)
                    self.assertFalse(any(path == '/v1/claim' for path, _ in calls))
                    self.assertEqual(w.state.events(), [event])
                    w.close()

    def test_lost_ack_bad_ack_and_oversize_keep_event_with_diagnostic(self):
        w = self.worker(); w.state.accept(self.task(w.state))
        w.state.finish('d1', {'ok': True})
        eid = w.state.events()[0]['event_id']
        for reply in [OSError('lost ACK'), {'ok': True}, {'ok': False, 'status': 'rejected'}]:
            with patch.object(w.client, 'request', side_effect=reply if isinstance(reply, Exception) else None,
                              return_value=reply):
                w.flush_events()
            self.assertEqual(w.state.events()[0]['event_id'], eid)
            self.assertTrue(w.state.status()['last_error'])
        with patch('aiui.agent.remote.MAX_BODY_BYTES', 20):
            w.flush_events()
        self.assertIn('超限', w.state.status()['last_error'])
        with patch.object(w.client, 'request', return_value={'ok': True, 'status': 'duplicate'}):
            w.flush_events()
        self.assertEqual(w.state.events(), [])

    def test_transport_rejects_unsafe_urls_and_redirects(self):
        for url in ['http://example.com', 'https://user:pass@example.com', 'file:///tmp/x',
                    'http://127.0.0.1:1/extra']:
            with self.assertRaises(ValueError):
                GatewayClient(url, 'n1', 'secret')
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        redirected_requests = []
        class Redirect(BaseHTTPRequestHandler):
            def do_POST(self):
                self.send_response(302); self.send_header('Location', '/target'); self.end_headers()
            def do_GET(self):
                redirected_requests.append(self.headers.get('Authorization'))
                self.send_response(200); self.end_headers(); self.wfile.write(b'{"ok":true}')
            def log_message(self, *a): pass
        server = ThreadingHTTPServer(('127.0.0.1', 0), Redirect)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close); self.addCleanup(server.shutdown)
        with self.assertRaises(Exception) as error:
            GatewayClient('http://127.0.0.1:%d' % server.server_port, 'n1', 'secret').request('/v1/register', {})
        self.assertNotIn('secret', str(error.exception))
        self.assertEqual(redirected_requests, [])

    def test_config_relative_files_and_token_are_private(self):
        (self.root / 'token').write_text('secret\n')
        path = self.root / 'node.json'
        path.write_text(json.dumps(dict(self.config, token_env=None, token_file='token', state_dir='state')))
        cfg = load_config(path)
        self.assertEqual(cfg['state_dir'], str(self.root / 'state'))
        w = RemoteWorker(cfg); self.addCleanup(w.close)
        self.assertNotIn('secret', json.dumps(w.state.status()))

    def test_process_lock_and_stable_instance_after_process_exit(self):
        state = NodeState(self.config['state_dir'])
        code = 'from aiui.agent.node_state import NodeState; NodeState(%r)' % self.config['state_dir']
        result = subprocess.run([sys.executable, '-B', '-c', code], capture_output=True)
        self.assertNotEqual(result.returncode, 0)
        instance = state.instance_id; state.close()
        state = NodeState(self.config['state_dir']); self.addCleanup(state.close)
        self.assertEqual(state.instance_id, instance)

    def test_process_crash_running_marker_recovers_as_unknown(self):
        code = '''
import os
from aiui.agent.node_state import NodeState
s = NodeState(%r)
s.accept(dict(task_id='t1', dispatch_id='d1', instance_id=s.instance_id, case={}, mcp={}))
s.mark_running('d1')
os._exit(7)
''' % self.config['state_dir']
        result = subprocess.run([sys.executable, '-B', '-c', code], capture_output=True)
        self.assertEqual(result.returncode, 7)
        w = self.worker()
        self.assertEqual(w.state.queued(), [])
        self.assertFalse(w.state.events()[0]['payload']['retryable'])

    def test_http_errors_keep_fixed_actionable_diagnostics(self):
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        class Rejected(BaseHTTPRequestHandler):
            def do_POST(self):
                self.send_response(413); self.end_headers(); self.wfile.write(b'secret')
            def log_message(self, *a): pass
        server = ThreadingHTTPServer(('127.0.0.1', 0), Rejected)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close); self.addCleanup(server.shutdown)
        with self.assertRaisesRegex(Exception, '超限'):
            GatewayClient('http://127.0.0.1:%d' % server.server_port, 'n1', 'secret').request('/v1/events', {})

    def test_check_cli_reports_safe_classified_failure(self):
        import contextlib
        import io
        output = io.StringIO()
        with patch('aiui.agent.remote.load_config', return_value=self.config), patch.object(
                RemoteWorker, 'check', side_effect=GatewayError('网关 HTTP 401：节点认证失败')), contextlib.redirect_stderr(output):
            self.assertEqual(main(['--config', 'unused', '--check']), 1)
        self.assertIn('401', output.getvalue())
        self.assertNotIn('secret', output.getvalue())


class LoopbackTests(unittest.TestCase):
    setUp = WorkerTests.setUp
    worker = WorkerTests.worker

    def test_replaced_worker_uploads_without_reclaiming_identity(self):
        try:
            from aiui.platform import remote, server, store
        except ModuleNotFoundError:
            self.skipTest('需要 aiui-platform 集成环境')
        for registered in (False, True):
            with self.subTest(registered=registered), tempfile.TemporaryDirectory(dir=self.root) as directory:
                root = Path(directory)
                patches = [patch(name, value) for name, value in {
                    'aiui.platform.server.AUDIT': root / 'audit.jsonl',
                    'aiui.platform.store.PROFILES_DIR': root / 'profiles',
                    'aiui.platform.remote.PROFILES_DIR': root / 'profiles',
                    'aiui.common.screenshots.SCREENSHOTS_DIR': root / 'screenshots',
                }.items()]
                for p in patches: p.start()
                db = store.Store(root / 'platform.db')
                policy = {'version': 1, 'nodes': {'n1': dict(name='Node', machine_id='m1',
                    token_sha256=hashlib.sha256(b'secret').hexdigest(), allowed_ips=['127.0.0.1/32'],
                    mcp_ids=['builtin-browser'], max_slots=1)}}
                platform = server.Platform(db)
                coordinator = remote.RemoteCoordinator(platform, policy)
                gateway = remote.make_gateway(coordinator, '127.0.0.1', 0)
                threading.Thread(target=gateway.serve_forever, daemon=True).start()
                w = RemoteWorker(dict(self.config, platform_url='http://127.0.0.1:%d' % gateway.server_port,
                                      state_dir=str(root / 'node')))
                try:
                    old = w._registration()
                    w.client.request('/v1/register', old)
                    tid = platform.add_tasks([dict(name='test', mcp_id='builtin-browser', target_node_id='n1')])[0]
                    task = w.client.request('/v1/claim', w._body(active_dispatch_ids=[]))['task']
                    w.state.accept(task)
                    w.state.logs(task['dispatch_id'], [{'ts': 1, 'level': 'info', 'source': 'agent', 'message': 'once'}])
                    w.state.finish(task['dispatch_id'], {'ok': True})
                    coordinator.mark_offline('n1')
                    replacement = dict(old, instance_id='replacement')
                    coordinator.register('n1', replacement)
                    w._registered = registered
                    w.step()
                    self.assertEqual(w.state.events(), [])
                    self.assertEqual(db.get_run(tid)['state'], 'done')
                    self.assertEqual(coordinator.list_nodes()[0]['instance_id'], 'replacement')
                    self.assertEqual(coordinator.list_nodes()[0]['running'], 0)
                    # 只补传模式即使新实例之后离线也不夺回身份。
                    coordinator.mark_offline('n1')
                    w.step()
                    self.assertEqual(coordinator.list_nodes()[0]['instance_id'], 'replacement')
                    w.close()
                    w = RemoteWorker(dict(self.config, platform_url='http://127.0.0.1:%d' % gateway.server_port,
                                          state_dir=str(root / 'node')))
                    w.step()
                    self.assertEqual(coordinator.list_nodes()[0]['instance_id'], 'replacement')
                finally:
                    w.close(); gateway.shutdown(); gateway.server_close(); db.close()
                    for p in reversed(patches): p.stop()
    def test_real_gateway_dry_run_execution_lost_ack_and_parent_restart(self):
        try:
            from aiui.platform import remote, server, store
        except ModuleNotFoundError:
            self.skipTest('需要 aiui-platform 集成环境')
        for name, value in {
            'aiui.platform.server.AUDIT': self.root / 'audit.jsonl',
            'aiui.platform.store.PROFILES_DIR': self.root / 'profiles',
            'aiui.platform.remote.PROFILES_DIR': self.root / 'profiles',
            'aiui.common.screenshots.SCREENSHOTS_DIR': self.root / 'screenshots',
        }.items():
            p = patch(name, value); p.start(); self.addCleanup(p.stop)
        db = store.Store(self.root / 'platform.db'); self.addCleanup(lambda: db.close())
        platform = server.Platform(db)
        policy = {'version': 1, 'nodes': {'n1': dict(name='Node', machine_id='m1',
                  token_sha256=hashlib.sha256(b'secret').hexdigest(), allowed_ips=['127.0.0.1/32'],
                  mcp_ids=['builtin-browser'], max_slots=1)}}
        coordinator = remote.RemoteCoordinator(platform, policy)
        gateway = remote.make_gateway(coordinator, '127.0.0.1', 0)
        threading.Thread(target=gateway.serve_forever, daemon=True).start()
        self.addCleanup(gateway.server_close); self.addCleanup(gateway.shutdown)
        self.config['platform_url'] = 'http://127.0.0.1:%d' % gateway.server_port
        w = self.worker()
        w.check(); self.assertEqual(coordinator.list_nodes(), [])
        tid = platform.add_tasks([dict(name='test', mcp_id='builtin-browser', steps=[], target_node_id='n1')])[0]
        async def execute(agent, task, reporter):
            reporter.log('loopback')
            return {'ok': True, 'steps': [], 'usage': {}}
        with patch('aiui.agent.remote.Agent._execute', execute):
            w.step(); w.agent.pool.shutdown(wait=True)
        # 真正提交成功后丢 ACK；保留同一 event_id，重新构造父平台后补传。
        original = w.client.request
        def lose(path, body):
            reply = original(path, body)
            if body.get('type') == 'result':
                raise OSError('ACK lost')
            return reply
        with patch.object(w.client, 'request', side_effect=lose): w.flush_events()
        self.assertTrue(w.state.events())
        # 网关持有旧 coordinator，替换其平台为同一持久库重建的对象。
        self.assertEqual(db.get_run(tid)['state'], 'done')
        db.close()
        db = store.Store(self.root / 'platform.db')
        db.recover_stale_runs()
        platform = server.Platform(db)
        rebuilt = remote.RemoteCoordinator(platform, policy)
        coordinator.__dict__.update(rebuilt.__dict__)
        w.flush_events()
        self.assertEqual(w.state.events(), [])
        self.assertEqual(db.get_run(tid)['state'], 'done')


class ProbeIsolationTests(unittest.TestCase):
    def test_probe_subprocess_failure_becomes_unavailable_capability(self):
        from aiui.agent.remote import RemoteWorker
        with tempfile.TemporaryDirectory() as tmp:
            config = dict(platform_url='http://127.0.0.1:1', node_id='n1', machine_id='m1',
                          name='Node', mcp_ids=['builtin-browser'], state_dir=str(Path(tmp) / 'node'),
                          token_env='TEST_REMOTE_TOKEN')
            with patch.dict(os.environ, TEST_REMOTE_TOKEN='secret'):
                worker = RemoteWorker(config)
                try:
                    with patch('aiui.agent.remote.subprocess.Popen', side_effect=OSError('boom')):
                        worker._probe_worker([{'id': 'builtin-browser'}])
                finally:
                    worker.close()
        self.assertEqual(len(worker._mcp_capabilities), 1)
        self.assertFalse(worker._mcp_capabilities[0]['available'])
        self.assertIn('OSError', worker._mcp_capabilities[0]['error'])

    def test_probe_module_accepts_empty_config(self):
        from aiui.agent import probe
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / 'configs.json'
            out = Path(tmp) / 'result.json'
            cfg.write_text('[]', encoding='utf-8')
            self.assertEqual(probe.main(['--config', str(cfg), '--output', str(out)]), 0)
            self.assertEqual(json.loads(out.read_text(encoding='utf-8')), [])


class CommandVersionTests(unittest.TestCase):
    @unittest.skipUnless(os.name == 'nt', 'Windows cmd wrapper')
    def test_cmd_batch_version_is_read(self):
        from aiui.agent import remote
        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp) / 'fake-tool.cmd'
            script.write_text('@echo 9.8.7\r\n', encoding='utf-8')
            with patch.object(remote.shutil, 'which', return_value=str(script)):
                self.assertEqual(remote._command_version('fake-tool', ['--version']), '9.8.7')


if __name__ == '__main__':
    unittest.main()
