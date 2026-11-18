"""主动领取任务的远程执行端；网络循环独立于 MCP/浏览器线程。"""
import argparse
import asyncio
import getpass
import importlib.metadata
import ipaddress
import json
import os
import platform
import re
import shutil
import ssl
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from aiui.agent.node_state import NodeState
from aiui.agent.runner import Agent
from aiui.common import mcp_runtime, profiles
from aiui.common.node_protocol import (
    VERSION, MAX_BODY_BYTES, ID_RE,
    sanitize_mcp_capabilities, sanitize_runtime, sanitize_slots_recommended,
)
from aiui.common.paths import ROOT, RUNTIME_DIR


class GatewayError(RuntimeError):
    pass


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class GatewayClient:
    def __init__(self, base_url, node_id, token, ca_file=None, allow_insecure_http=False):
        url = urllib.parse.urlsplit(base_url)
        if (url.scheme not in ('https', 'http') or not url.hostname or url.username is not None
                or url.password is not None or url.query or url.fragment or url.path not in ('', '/')):
            raise ValueError('网关地址必须是无用户信息的 HTTP(S) 根地址')
        try:
            loopback = ipaddress.ip_address(url.hostname).is_loopback
        except ValueError:
            # 只允许明确的 localhost；任意 DNS 名不能作为回环安全证明。
            loopback = url.hostname.lower() == 'localhost'
        if url.scheme == 'http' and not loopback and not allow_insecure_http:
            raise ValueError('非回环 HTTP 需要明确 allow_insecure_http')
        if not isinstance(node_id, str) or not ID_RE.fullmatch(node_id):
            raise ValueError('节点 ID 无效')
        if not isinstance(token, str) or not token or any(c.isspace() for c in token):
            raise ValueError('节点凭证为空或格式无效')
        self.base_url = base_url.rstrip('/')
        self.node_id = node_id
        self._token = token
        context = ssl.create_default_context(cafile=ca_file)
        self._opener = urllib.request.build_opener(_NoRedirect(), urllib.request.HTTPSHandler(context=context))

    def request(self, path, body):
        if path not in ('/v1/register', '/v1/heartbeat', '/v1/claim', '/v1/events'):
            raise ValueError('网关接口无效')
        data = json.dumps(body, ensure_ascii=False).encode('utf-8')
        if len(data) > MAX_BODY_BYTES:
            raise GatewayError('回传数据超限（64 MiB），本地事件已保留')
        req = urllib.request.Request(self.base_url + path, data=data, headers={
            'Content-Type': 'application/json', 'Authorization': 'Bearer ' + self._token,
            'X-AIUI-Node-ID': self.node_id})
        try:
            with self._opener.open(req, timeout=8) as response:
                raw = response.read(MAX_BODY_BYTES + 1)
                if len(raw) > MAX_BODY_BYTES:
                    raise GatewayError('网关响应超限')
                result = json.loads(raw)
        except urllib.error.HTTPError as exc:
            # 不回显服务端正文、URL 或请求头，避免代理反射凭证。
            code = exc.code
            safe_errors = {
                '另一节点实例仍在线', '实例未注册或已被替换', '节点机器不符合策略',
                '节点机器授权已变更，请重新注册', '未知任务或分配归属不匹配',
                '事件 ID 冲突', '执行批次已完成', '节点未授权',
            }
            try:
                error = json.loads(exc.read(4096)).get('error')
            except Exception:
                error = None
            exc.close()
            detail = {400: '协议或分配被拒绝', 401: '节点认证失败',
                      403: '节点来源或权限被拒绝', 413: '回传数据超限'}.get(code, '请求未确认')
            if isinstance(error, str) and error in safe_errors:
                detail = error
            raise GatewayError('网关 HTTP %s：%s；本地事件已保留' % (code, detail)) from None
        except urllib.error.URLError as exc:
            if isinstance(exc.reason, ssl.SSLCertVerificationError):
                raise GatewayError('网关证书校验失败，请检查 CA、证书链和主机名') from None
            raise GatewayError('无法连接网关，请检查地址、DNS、端口和防火墙') from None
        except ssl.SSLCertVerificationError:
            raise GatewayError('网关证书校验失败，请检查 CA、证书链和主机名') from None
        except TimeoutError:
            raise GatewayError('网关请求超时，本地事件已保留') from None
        except (OSError, ValueError):
            raise GatewayError('网关连接或 JSON 响应无效') from None
        if not isinstance(result, dict) or result.get('ok') is not True:
            raise GatewayError('网关拒绝请求，事件已保留')
        return result


def load_config(path):
    path = Path(path).resolve()
    config = json.loads(path.read_text(encoding='utf-8-sig'))
    if not isinstance(config, dict):
        raise ValueError('配置必须是 JSON 对象')
    for key in ('state_dir', 'token_file', 'ca_file', 'chromium'):
        if config.get(key):
            config[key] = str((path.parent / config[key]).resolve())
    return config


class DurableReporter:
    def __init__(self, state, dispatch_id):
        self.state, self.dispatch_id = state, dispatch_id

    def log(self, message, level='info', source='agent'):
        self.state.logs(self.dispatch_id, [dict(ts=round(time.time(), 3), level=level,
                                               source=source, message=str(message)[:8000])])

    def close(self):
        pass  # log() 返回前已同步提交，无异步批次需要等待。


def _version_file():
    try:
        return (ROOT / 'VERSION').read_text(encoding='utf-8-sig').strip()[:200]
    except OSError:
        return ''


def _command_version(command, args, timeout=3):
    executable = shutil.which(command)
    if os.name == 'nt' and executable and not executable.lower().endswith(
            ('.exe', '.cmd', '.bat', '.com')):
        for suffix in ('.cmd', '.exe', '.bat'):
            candidate = shutil.which(command + suffix)
            if candidate:
                executable = candidate
                break
    if not executable:
        return ''
    invocation = [executable, *args]
    if executable.lower().endswith(('.cmd', '.bat')):
        invocation = ['cmd.exe', '/c', 'call', executable, *args]
    try:
        kwargs = {}
        if os.name == 'nt':
            kwargs['creationflags'] = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
        done = subprocess.run(invocation, capture_output=True, timeout=timeout, **kwargs)
        raw = done.stdout or done.stderr or b''
        text = raw.decode('utf-8', 'replace').strip()
        return text.splitlines()[0][:200] if text else ''
    except Exception:
        return ''


def _package_version(name):
    try:
        return importlib.metadata.version(name)[:200]
    except Exception:
        return ''


def _memory_mb():
    try:
        if os.name == 'nt':
            import ctypes

            class MemoryStatusEx(ctypes.Structure):
                _fields_ = [
                    ('dwLength', ctypes.c_ulong),
                    ('dwMemoryLoad', ctypes.c_ulong),
                    ('ullTotalPhys', ctypes.c_ulonglong),
                    ('ullAvailPhys', ctypes.c_ulonglong),
                    ('ullTotalPageFile', ctypes.c_ulonglong),
                    ('ullAvailPageFile', ctypes.c_ulonglong),
                    ('ullTotalVirtual', ctypes.c_ulonglong),
                    ('ullAvailVirtual', ctypes.c_ulonglong),
                    ('ullAvailExtendedVirtual', ctypes.c_ulonglong),
                ]

            status = MemoryStatusEx()
            status.dwLength = ctypes.sizeof(MemoryStatusEx)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
                return int(status.ullTotalPhys // (1024 * 1024))
            return None
        return int(os.sysconf('SC_PAGE_SIZE') * os.sysconf('SC_PHYS_PAGES') // (1024 * 1024))
    except Exception:
        return None


def _recommend_slots(cpu_cores, memory_mb):
    if not cpu_cores:
        return 1
    if memory_mb and memory_mb < 4096:
        return 1
    return max(1, min(2, int(cpu_cores) // 2 or 1))


def _mcp_version(mcp):
    for arg in mcp.get('args') or []:
        match = re.search(r'@[\w./-]+@([0-9][\w.\-]*)', str(arg))
        if match:
            return match.group(1)[:200]
    return ''


class RemoteWorker:
    def __init__(self, config):
        self.config = dict(config)
        self._started_at = time.time()
        for key in ('node_id', 'machine_id', 'name', 'state_dir', 'platform_url'):
            if not isinstance(config.get(key), str) or not config[key].strip():
                raise ValueError('节点配置缺少必要字段：' + key)
        slots = config.get('slots', 1)
        if type(slots) is not int or not 1 <= slots <= 128:
            raise ValueError('slots 必须为 1–128')
        if (not isinstance(config.get('mcp_ids'), list) or not config['mcp_ids']
                or not all(isinstance(x, str) and x for x in config['mcp_ids'])):
            raise ValueError('mcp_ids 无效')
        if bool(config.get('token_file')) == bool(config.get('token_env')):
            raise ValueError('必须且只能配置 token_file 或 token_env')
        token = (Path(config['token_file']).read_text(encoding='utf-8-sig').strip()
                 if config.get('token_file') else os.environ.get(config['token_env'], '').strip())
        self.client = GatewayClient(config['platform_url'], config['node_id'], token,
                                    config.get('ca_file'), config.get('allow_insecure_http', False))
        self.state = NodeState(config['state_dir'])
        try:
            self.state.bind(config['node_id'], config['machine_id'])
            self.state.recover()
        except BaseException:
            self.state.close()
            raise
        self.agent = Agent(config['platform_url'], config['node_id'], '127.0.0.1', 0,
                           slots, config.get('chromium'))
        self.slots = slots
        self.stop_flag = threading.Event()
        self._registered = False
        self._upload_only = self.state.status()['upload_only']
        self._heartbeat_at = 0
        self._interval = 3
        self._futures = {}
        self._closed = False
        self._runtime = self._describe_runtime()
        resources = self._runtime.get('resources') or {}
        self._slots_recommended = sanitize_slots_recommended(
            _recommend_slots(resources.get('cpu_cores'), resources.get('memory_mb')))
        self._mcp_configs = []
        self._mcp_capabilities = []
        self._probe_signature = None
        self._probe_thread = None
        self._probe_lock = threading.Lock()

    def _describe_runtime(self):
        chrome = ''
        candidates = []
        if self.config.get('chromium'):
            candidates.append(Path(self.config['chromium']))
        if os.environ.get('CHROMIUM_EXE'):
            candidates.append(Path(os.environ['CHROMIUM_EXE']))
        for base, suffix in (
                (os.environ.get('PROGRAMFILES', r'C:\Program Files'), 'Google/Chrome/Application/chrome.exe'),
                (os.environ.get('PROGRAMFILES(X86)', r'C:\Program Files (x86)'), 'Google/Chrome/Application/chrome.exe'),
                (os.environ.get('LOCALAPPDATA', ''), 'Google/Chrome/Application/chrome.exe')):
            if base:
                candidates.append(Path(base) / suffix)
        for candidate in candidates:
            try:
                if candidate.is_file():
                    chrome = str(candidate)[:200]
                    break
            except OSError:
                continue
        llm = {}
        try:
            from aiui.common.llm import load_llm_config
            cfg = load_llm_config()
            llm = {
                'configured': bool(cfg.get('api_key')),
                'provider': str(cfg.get('provider') or '')[:200],
                'model': str(cfg.get('model') or '')[:200],
                'base_url': str(cfg.get('base_url') or '')[:200],
            }
        except Exception:
            llm = {'configured': False}
        cpu_cores = os.cpu_count()
        try:
            disk_free = int(shutil.disk_usage(str(RUNTIME_DIR)).free // (1024 * 1024))
        except OSError:
            disk_free = None
        runtime = {
            'agent_version': _version_file(),
            'protocol': VERSION,
            'os': platform.system(),
            'os_version': platform.version(),
            'arch': platform.machine(),
            'python': platform.python_version(),
            'node': _command_version('node', ['--version']),
            'npm': _command_version('npm', ['--version']),
            'playwright': _package_version('playwright'),
            'chrome': chrome,
            'chrome_detected': bool(chrome),
            'chromium_configured': bool(self.config.get('chromium')),
            'account': getpass.getuser(),
            'scheduled_task': os.environ.get('AIUI_SCHEDULED_TASK', ''),
            'started_at': self._started_at,
            'llm': llm,
            'resources': {
                'cpu_cores': cpu_cores,
                'memory_mb': _memory_mb(),
                'disk_free_mb': disk_free,
            },
        }
        return sanitize_runtime(runtime)

    def _body(self, **extra):
        return dict(version=VERSION, node_id=self.config['node_id'],
                    instance_id=self.state.instance_id, **extra)

    def _registration(self, dry_run=False):
        extras = {}
        if self._runtime:
            extras['runtime'] = self._runtime
        if self._slots_recommended is not None:
            extras['slots_recommended'] = self._slots_recommended
        if dry_run:
            extras['dry_run'] = True
        return self._body(machine_id=self.config['machine_id'], name=self.config['name'],
                          os=platform.system(), slots=self.config.get('slots', 1),
                          mcp_ids=self.config['mcp_ids'], features=['env_profile'], **extras)

    def _start_capability_probe(self):
        configs = [c for c in self._mcp_configs if isinstance(c, dict) and c.get('id')]
        if not configs:
            return
        signature = json.dumps(configs, sort_keys=True, ensure_ascii=False)
        with self._probe_lock:
            if self._probe_thread and self._probe_thread.is_alive():
                return
            if signature == self._probe_signature and self._mcp_capabilities:
                return
            self._probe_signature = signature
            self._probe_thread = threading.Thread(
                target=self._probe_worker, args=(configs,), daemon=True)
            self._probe_thread.start()

    def _probe_worker(self, configs):
        results = []
        for mcp in configs:
            mcp_id = str(mcp.get('id') or '')
            if not mcp_id:
                continue
            tools, error = [], ''
            try:
                tools, error = mcp_runtime.probe_tools(mcp, ROOT, timeout=60)
            except Exception as exc:
                error = f'{type(exc).__name__}: {exc}'
            names = [t.get('name') for t in tools
                     if isinstance(t, dict) and t.get('name')]
            cap = {
                'id': mcp_id,
                'available': not bool(error),
                'tool_count': len(names),
                'tools': names[:128],
                'profile_capable': profiles.detect_adapter(mcp) is not None,
                'probed_at': time.time(),
            }
            version = _mcp_version(mcp)
            if version:
                cap['version'] = version
            if error:
                cap['error'] = str(error)[:500]
            results.append(cap)
        self._mcp_capabilities = sanitize_mcp_capabilities(results)

    def _heartbeat_extras(self):
        with self._probe_lock:
            capabilities = list(self._mcp_capabilities)
        return {'mcp_capabilities': capabilities} if capabilities else {}

    def check(self):
        self.client.request('/v1/register', self._registration(dry_run=True))
        chromium = self.config.get('chromium')
        if chromium and not Path(chromium).is_file():
            raise ValueError('配置的 chromium 文件不存在')
        # 只报告环境事实；不读取/回显本机 LLM 配置与 Profile 内容。
        return dict(ok=True, python=platform.python_version(), os=platform.system(),
                    node_available=bool(shutil.which('node')), npx_available=bool(shutil.which('npx')),
                    chromium_configured=bool(chromium), runtime=self._runtime,
                    slots_recommended=self._slots_recommended, state=self.state.status())

    def flush_events(self):
        # 每轮有界上传，下一轮可及时执行心跳；确认顺序始终按 durable seq。
        started = time.monotonic()
        for index, event in enumerate(self.state.events()):
            if index >= 16 or (index and time.monotonic() - started >= 1.0):
                break
            try:
                body = self._body(**event)
                if len(json.dumps(body, ensure_ascii=False).encode('utf-8')) > MAX_BODY_BYTES:
                    raise GatewayError('回传数据超限（64 MiB），本地事件已保留')
                reply = self.client.request('/v1/events', body)
                if reply.get('ok') is not True or reply.get('status') not in ('received', 'duplicate'):
                    raise GatewayError('事件确认无效或被拒绝，本地事件已保留')
            except Exception as exc:
                message = str(exc) if isinstance(exc, GatewayError) else '事件传输失败，本地事件已保留'
                self.state.diagnostic(message, event['event_id'])
                return False
            self.state.acknowledge(event['event_id'])
        return True

    def _execute(self, task):
        started = time.monotonic()
        reporter = DurableReporter(self.state, task['dispatch_id'])
        result = dict(task_id=task['task_id'], dispatch_id=task['dispatch_id'],
                      agent_id=self.config['node_id'], ok=False, steps=[], retryable=False)
        try:
            result.update(asyncio.run(self.agent._execute(task, reporter)))
        except Exception:
            # 执行器已有细粒度日志；异常文本可能包含 MCP 凭证，不直接回显。
            result.update(ok=False, error='执行异常，请检查节点私有环境配置', retryable=False)
        result['elapsed_ms'] = int((time.monotonic() - started) * 1000)
        reporter.close()
        self.state.finish(task['dispatch_id'], result)

    def _start_queued(self):
        self._futures = {did: future for did, future in self._futures.items() if not future.done()}
        for task in self.state.queued():
            if len(self._futures) >= self.slots or self.stop_flag.is_set():
                break
            if self.state.mark_running(task['dispatch_id']):
                self._futures[task['dispatch_id']] = self.agent.pool.submit(self._execute, task)

    def step(self):
        if self._closed:
            raise RuntimeError('节点已关闭')
        if not self._upload_only:
            self._start_queued()
        try:
            # 原 dispatch 的补传权限独立于当前在线实例；认证/归属仍由 events 校验。
            uploaded = self.flush_events()
            if self._upload_only:
                return uploaded
            if not self._registered:
                response = self.client.request('/v1/register', self._registration())
                self.slots = min(self.config.get('slots', 1), int(response.get('slots', self.slots)))
                self._mcp_configs = [m for m in (response.get('mcps') or [])
                                     if isinstance(m, dict) and m.get('id')]
                self._start_capability_probe()
                self._registered = True
            if time.monotonic() >= self._heartbeat_at:
                self.client.request('/v1/heartbeat', self._body(
                    active_dispatch_ids=self.state.active_ids(), **self._heartbeat_extras()))
                self._heartbeat_at = time.monotonic() + self._interval
            if not uploaded:
                return False
            if len(self.state.active_ids()) < self.slots:
                response = self.client.request('/v1/claim', self._body(active_dispatch_ids=self.state.active_ids()))
                task = response.get('task')
                if task is not None:
                    self.state.accept(task)
                    self._start_queued()
            return True
        except Exception as exc:
            self._registered = False
            if isinstance(exc, GatewayError) and any(reason in str(exc) for reason in (
                    '另一节点实例仍在线', '实例未注册或已被替换')):
                self._upload_only = True
                self.state.restrict_to_uploads()
            self.state.diagnostic(str(exc) if isinstance(exc, GatewayError) else '节点协议或连接失败')
            return False

    def run(self):
        backoff = 0.25
        last_reported = None
        try:
            while not self.stop_flag.is_set():
                success = self.step()
                if not success:
                    error = self.state.status()['last_error']
                    if error != last_reported:
                        print('[remote] ' + error, file=sys.stderr, flush=True)
                        last_reported = error
                else:
                    last_reported = None
                backoff = 0.25 if success else min(backoff * 2, 10)
                if self._registered:
                    backoff = min(backoff, self._interval)
                self.stop_flag.wait(backoff)
        finally:
            self.close()

    def close(self):
        if self._closed:
            return
        self._closed = True
        self.stop_flag.set()
        # 等执行线程写完 durable outbox 再释放目录锁。
        self.agent.pool.shutdown(wait=True)
        self.state.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description='远程执行节点')
    parser.add_argument('--config', required=True)
    parser.add_argument('--check', action='store_true')
    args = parser.parse_args(argv)
    worker = None
    try:
        worker = RemoteWorker(load_config(args.config))
        if args.check:
            print(json.dumps(worker.check(), ensure_ascii=False))
        else:
            worker.run()
        return 0
    except KeyboardInterrupt:
        return 0
    except GatewayError as exc:
        print('远程节点启动或自检失败：' + str(exc), file=sys.stderr)
        return 1
    except Exception as exc:
        if isinstance(exc, FileNotFoundError):
            detail = '本机配置、token、CA 或浏览器文件不存在'
        elif isinstance(exc, PermissionError):
            detail = '本机文件或状态目录权限不足'
        elif isinstance(exc, ssl.SSLError):
            detail = '本机 CA 文件或 TLS 配置无效'
        elif isinstance(exc, (ValueError, TypeError)):
            detail = '本机配置格式、必要字段或凭证来源无效'
        elif isinstance(exc, RuntimeError):
            detail = '状态目录可能已由另一进程占用'
        else:
            detail = '本机初始化失败，请检查运行环境'
        print('远程节点启动或自检失败（%s）：%s' % (type(exc).__name__, detail), file=sys.stderr)
        return 1
    finally:
        if worker:
            worker.close()


if __name__ == '__main__':
    raise SystemExit(main())
