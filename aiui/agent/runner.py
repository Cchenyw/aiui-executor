# -*- coding: utf-8 -*-
"""执行机 Agent（执行面的入口，端口默认 8001）

关键变化（相对最初的调度 demo）：
  · 任务自带 MCP 配置：平台按用例选择 MCP，执行机按任务拉起对应会话
    （内置浏览器 MCP / 官方 Playwright MCP / 自定义 stdio·sse·http）。
  · MCP stderr 捕获：每个任务把 MCP 子进程日志写入临时文件并实时转发到平台，
    平台执行详情页可以像看 CI 日志一样查看。
  · 执行日志批量上报：agent 自己的步骤日志 + MCP 子进程日志统一 POST /api/logs。
  · 步骤断言：除内置的 expect_text 外，任意 MCP 步骤都可带
    assert_contains / assert_not_contains，对工具返回文本做断言。

仍然守住的约束：
  · 一任务一子进程一浏览器，任务之间强隔离，失败可强杀；
  · 线程池管任务、进程管工具层；平台侧再做一次容量兜底；
  · stdout 是 MCP 协议通道，执行机日志一律走 stderr / 平台日志接口。
"""
import argparse
import asyncio
import json
import os
import queue
import shutil
import socket
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from aiui.agent import ai_executor
from aiui.common import mcp_runtime, profiles, screenshots
from aiui.common.paths import ROOT

FALLBACK_MCP = {
    "id": "builtin-browser", "name": "内置浏览器 MCP", "transport": "stdio",
    "command": "{python}", "args": ["-m", "aiui.toolservers.builtin_browser"],
    "env": {}, "url": "", "headers": {},
}

OFFICIAL_PLAYWRIGHT_MCP = "@playwright/mcp"


def ensure_mcp_isolation(mcp):
    """并发执行时保证官方 Playwright MCP 使用独立浏览器 profile。

    每个任务本来就会建立独立的 stdio MCP 会话和子进程；官方 Playwright MCP
    默认复用持久 profile，多个实例会互相影响，因此缺少 --isolated 时自动补上。
    内置 MCP 每个进程自行启动临时浏览器，其它 MCP 保持配置原样。
    """
    if not isinstance(mcp, dict) or (mcp.get("transport") or "stdio").lower() != "stdio":
        return mcp
    args = [str(a) for a in (mcp.get("args") or [])]
    text = " ".join([str(mcp.get("command") or ""), *args]).lower()
    if OFFICIAL_PLAYWRIGHT_MCP not in text or "--isolated" in args:
        return mcp
    out = dict(mcp)
    out["args"] = list(mcp.get("args") or []) + ["--isolated"]
    return out


def log(*a):
    print("[agent]", *a, file=sys.stderr, flush=True)


def post_json(url, payload, timeout=5):
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8") or "{}")


class LogReporter:
    """按任务收集日志，批量上报平台；close() 会同步刷完，保证结果先有日志。"""

    def __init__(self, platform, agent_id, task_id):
        self.platform = platform.rstrip("/")
        self.agent_id = agent_id
        self.task_id = task_id
        self.q = queue.Queue()
        self.stop_flag = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True,
                                       name=f"log-{task_id}")
        self.thread.start()

    def log(self, message, level="info", source="agent"):
        msg = str(message)
        if len(msg) > 8000:
            msg = msg[:8000] + " …(截断)"
        self.q.put({"ts": round(time.time(), 3), "level": level,
                    "source": source, "message": msg})
        print(f"[{source}] [{level}] {msg[:500]}", file=sys.stderr, flush=True)

    def _flush(self, batch):
        if not batch:
            return
        try:
            post_json(self.platform + "/api/logs", {
                "agent_id": self.agent_id, "task_id": self.task_id, "logs": batch,
            }, timeout=5)
        except Exception as e:
            print(f"[agent] 日志上报失败（{len(batch)} 条）: {e}", file=sys.stderr, flush=True)

    def _run(self):
        while not self.stop_flag.is_set() or not self.q.empty():
            batch = []
            try:
                batch.append(self.q.get(timeout=0.3))
            except queue.Empty:
                continue
            while len(batch) < 100:
                try:
                    batch.append(self.q.get_nowait())
                except queue.Empty:
                    break
            self._flush(batch)

    def close(self):
        self.stop_flag.set()
        try:
            self.thread.join(timeout=5)
        except Exception:
            pass
        rest = []
        while len(rest) < 2000:
            try:
                rest.append(self.q.get_nowait())
            except queue.Empty:
                break
        if rest:
            self._flush(rest)


class StderrTail:
    """tail 一个正在被 MCP 子进程写入的临时文件，逐行转成平台日志。"""

    def __init__(self, path, reporter):
        self.path = path
        self.reporter = reporter
        self.pos = 0
        self.buf = ""
        self.stop_flag = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True, name="mcp-stderr")
        self.thread.start()

    def _pump(self):
        try:
            with open(self.path, "rb") as f:
                f.seek(self.pos)
                data = f.read()
                if not data:
                    return
                self.pos = f.tell()
        except OSError:
            return
        self.buf += data.decode("utf-8", "replace")
        lines = self.buf.split("\n")
        self.buf = lines.pop()
        for line in lines:
            line = line.strip()
            if not line:
                continue
            level = "error" if ("error" in line.lower() or "traceback" in line.lower()) else "info"
            self.reporter.log(line, level=level, source="mcp")

    def _run(self):
        while not self.stop_flag.is_set():
            self._pump()
            self.stop_flag.wait(0.15)

    def close(self):
        self.stop_flag.set()
        try:
            self.thread.join(timeout=3)
        except Exception:
            pass
        self._pump()
        if self.buf.strip():
            self.reporter.log(self.buf.strip(), source="mcp")
            self.buf = ""


async def _capture_profile(session, tools, task, result, reporter):
    """采集 run 成功后导出 storage state；失败时把任务置为失败。"""
    capture = task.get("profile_capture") or {}
    if not capture or not result.get("ok"):
        return result
    try:
        state = await profiles.capture(session, tools)
        state, summary = profiles.validate_state(state)
        result["profile_state"] = state
        result["profile_summary"] = summary
        reporter.log(
            f"环境采集成功：cookies={summary['cookie_count']} "
            f"origins={summary['origin_count']} "
            f"localStorage={summary['local_storage_keys']}", source="profile")
    except Exception as e:
        result["ok"] = False
        result["error"] = f"环境采集失败: {mcp_runtime.describe_exc(e)}"
        result["retryable"] = False
        reporter.log(result["error"], level="error", source="profile")
    return result


class Agent:
    def __init__(self, platform, agent_id, host, port, slots, chromium):
        self.platform = platform.rstrip("/")
        self.agent_id = agent_id
        self.host = host
        self.port = port
        self.slots = slots
        self.chromium = chromium
        self.running = 0
        self.lock = threading.Lock()
        self.pool = ThreadPoolExecutor(max_workers=slots, thread_name_prefix="case")
        self.stop_flag = threading.Event()

    # ── 注册 & 心跳 ────────────────────────────────────────────────
    def register(self):
        for i in range(20):
            try:
                post_json(self.platform + "/api/agents/register", {
                    "agent_id": self.agent_id, "host": self.host, "port": self.port,
                    "slots": self.slots, "tags": ["local", "chromium"],
                    "features": ["env_profile"],
                })
                log(f"注册成功 -> {self.platform}  容量={self.slots}")
                return True
            except Exception as e:
                log(f"注册失败({i+1}/20): {e}")
                time.sleep(1)
        return False

    def heartbeat_loop(self):
        while not self.stop_flag.is_set():
            try:
                post_json(self.platform + "/api/agents/heartbeat", {
                    "agent_id": self.agent_id, "running": self.running}, timeout=3)
            except Exception:
                pass
            self.stop_flag.wait(3)

    # ── 接任务 ─────────────────────────────────────────────────────
    def accept(self, task):
        with self.lock:
            if self.running >= self.slots:
                return False, "容量已满"
            self.running += 1
        self.pool.submit(self._run, task)
        return True, "已接受"

    def _run(self, task):
        t0 = time.time()
        tid = task.get("task_id")
        reporter = LogReporter(self.platform, self.agent_id, tid)
        res = {"task_id": tid, "agent_id": self.agent_id,
               "dispatch_id": task.get("dispatch_id"),
               "ok": False, "elapsed_ms": 0, "steps": [], "error": "",
               "final_text": "", "mcp_id": (task.get("mcp") or {}).get("id", "")}
        try:
            res.update(asyncio.run(self._execute(task, reporter)))
        except Exception as e:
            res["error"] = mcp_runtime.describe_exc(e)
            reporter.log(f"任务异常: {res['error']}", level="error")
        res["elapsed_ms"] = int((time.time() - t0) * 1000)
        reporter.log(f"任务{'成功' if res.get('ok') else '失败'}，耗时 {res['elapsed_ms']} ms",
                     level="info" if res.get("ok") else "error")
        reporter.close()                       # 先把日志刷到平台，再回传结果
        with self.lock:
            self.running -= 1
        for attempt in range(3):
            try:
                post_json(self.platform + "/api/results", res, timeout=8)
                break
            except Exception as e:
                log(f"结果回传失败({attempt+1}/3): {e}")
                time.sleep(0.5 * (attempt + 1))

    async def _execute(self, task, reporter):
        """一个任务 = 一个 MCP 会话 + 一个独立浏览器（取决于 MCP 实现）。"""
        case = task.get("case") or {}
        mcp = task.get("mcp") or FALLBACK_MCP
        isolated = ensure_mcp_isolation(mcp)
        if isolated is not mcp:
            reporter.log("并发隔离：官方 Playwright MCP 未配置 --isolated，已自动追加",
                         source="mcp")
            mcp = isolated
        if self.chromium:
            mcp = dict(mcp)
            env = dict(mcp.get("env") or {})
            env.setdefault("CHROMIUM_EXE", self.chromium)
            mcp["env"] = env

        steps = case.get("steps") or []
        mode = (case.get("mode") or "scripted").lower()
        timeout = float(case.get("timeout_s", 60))
        deadline = time.monotonic() + timeout
        out = {"ok": False, "steps": [], "error": "", "final_text": "",
               "mcp_id": mcp.get("id", "")}
        reporter.log(f"开始执行 MCP={mcp.get('id') or mcp.get('name')} "
                     f"mode={mode} transport={mcp.get('transport', 'stdio')} steps={len(steps)}")

        profile_dir = None
        profile = task.get("profile") or {}
        if profile:
            if not profiles.detect_adapter(mcp):
                out["error"] = "当前 MCP 不支持执行环境 Profile 注入"
                out["retryable"] = False
                reporter.log(out["error"], level="error", source="profile")
                return out
            try:
                state, summary = profiles.validate_state(profile.get("state"))
                profile_dir = tempfile.mkdtemp(prefix="aiui-profile-")
                state_path = os.path.join(profile_dir, "state.json")
                with open(state_path, "w", encoding="utf-8") as f:
                    json.dump(state, f, ensure_ascii=False, sort_keys=True,
                              separators=(",", ":"))
                mcp = profiles.apply_to_mcp(mcp, state_path)
                reporter.log(
                    f"已注入执行环境 Profile {profile.get('profile_id','')}"
                    f"@v{profile.get('version',0)} (hash={str(profile.get('hash',''))[:12]}, "
                    f"cookies={summary['cookie_count']}, origins={summary['origin_count']})",
                    source="profile")
            except Exception as e:
                if profile_dir:
                    shutil.rmtree(profile_dir, ignore_errors=True)
                out["error"] = f"执行环境 Profile 注入失败: {mcp_runtime.describe_exc(e)}"
                out["retryable"] = False
                reporter.log(out["error"], level="error", source="profile")
                return out

        errlog = None
        tail = None
        err_path = None
        if (mcp.get("transport") or "stdio").lower() == "stdio":
            try:
                fd, err_path = tempfile.mkstemp(prefix="mcp-stderr-", suffix=".log")
                errlog = os.fdopen(fd, "wb", buffering=0)
                tail = StderrTail(err_path, reporter)
            except Exception as e:
                reporter.log(f"无法创建 MCP 日志临时文件: {e}", level="warn")

        async def body():
            async with mcp_runtime.open_session(mcp, ROOT, errlog=errlog) as session:
                tools = await session.list_tools()
                names = {t.name for t in tools.tools}
                reporter.log(f"MCP 已连接，工具 {len(names)} 个: "
                             f"{', '.join(sorted(names)[:12])}{' …' if len(names) > 12 else ''}")
                if mode == "ai":
                    remaining = max(5.0, deadline - time.monotonic() - 2)
                    ai_out = await ai_executor.run_ai_task(
                        session, tools.tools, case, reporter, remaining,
                        progress=out, budget=task.get("budget"))
                    ai_out["mcp_id"] = mcp.get("id", "")
                    return await _capture_profile(session, tools.tools, task,
                                                  ai_out, reporter)
                # scripted 工具名属于所选 MCP 的契约，换 MCP/重新探测不会转换步骤。
                # 先检查全部步骤，防止执行到一半才遇到错配并重复产生副作用。
                missing = [f"第 {i} 步 {step.get('tool')}"
                           for i, step in enumerate(steps, 1)
                           if step.get("tool") not in names]
                if missing:
                    out["error"] = (
                        f"步骤与执行 MCP 不匹配：MCP {mcp.get('id') or mcp.get('name') or '当前 MCP'}"
                        f" 不提供以下工具：{'；'.join(missing)}。"
                        "请使用该 MCP 对应的步骤示例，或修改工具名和参数；"
                        "重新探测或切换 MCP 不会自动转换已保存的步骤。")
                    out["retryable"] = False
                    reporter.log(out["error"], level="error")
                    return out
                for i, step in enumerate(steps, 1):
                    tool = step.get("tool")
                    args = step.get("args") or {}
                    rec = {"tool": tool, "args": args, "ok": False, "result": ""}
                    rec["screenshot"] = {"error": "步骤尚未完成，未能采集截图"}
                    out["steps"].append(rec)
                    if tool == profiles.BUILTIN_CAPTURE_TOOL:
                        out["error"] = "profile_capture 是内部工具，禁止作为用例步骤调用"
                        rec["result"] = out["error"]
                        reporter.log(out["error"], level="error", source="profile")
                        return out
                    arg_preview = json.dumps(args, ensure_ascii=False)
                    reporter.log(f"[{i}/{len(steps)}] 调用 {tool} args={arg_preview[:600]}")
                    try:
                        step_timeout = min(timeout, max(1.0, float(step.get("timeout_s", 60))))
                        text, is_error = await mcp_runtime.call_tool(
                            session, tool, args, timeout=step_timeout)
                    except asyncio.CancelledError:
                        rec["result"] = "任务超时或执行被取消"
                        rec["screenshot"] = {"error": "任务超时或执行被取消，未能采集截图"}
                        raise
                    except Exception as e:
                        text = mcp_runtime.describe_exc(e)
                        is_error = True
                    rec["result"] = text

                    obj = {}
                    stripped = (text or "").strip()
                    if stripped.startswith("{"):
                        try:
                            obj = json.loads(stripped)
                            if not isinstance(obj, dict):
                                obj = {}
                        except Exception:
                            obj = {}

                    error = ""
                    if is_error:
                        error = text or f"{tool} 返回 isError"
                    elif obj.get("error"):
                        error = str(obj["error"])
                    elif "ok" in obj and obj["ok"] is False:
                        error = "断言未通过: " + str(obj.get("actual", ""))[:200]
                    elif step.get("assert_contains") and step["assert_contains"] not in (text or ""):
                        error = f"断言未通过：返回文本不包含 {step['assert_contains']!r}"
                    elif step.get("assert_not_contains") and step["assert_not_contains"] in (text or ""):
                        error = f"断言未通过：返回文本包含 {step['assert_not_contains']!r}"

                    rec["ok"] = not bool(error)
                    rec["screenshot"] = await screenshots.capture_step(
                        session, tools.tools, timeout=max(0.0, deadline - time.monotonic() - 0.25))
                    if rec["screenshot"].get("error"):
                        reporter.log(rec["screenshot"]["error"], level="warn", source="screenshot")
                    if error:
                        out["error"] = error
                        reporter.log(f"[{i}/{len(steps)}] {tool} 失败: {error[:800]}", level="error")
                        return out

                    if tool == "page_text" and obj.get("text"):
                        out["final_text"] = str(obj["text"])[:2000]
                    elif str(tool).endswith("get_visible_text") and text:
                        out["final_text"] = text[:2000]
                    reporter.log(f"[{i}/{len(steps)}] {tool} 成功: {(text or '')[:500]}")
                out["ok"] = True
                return await _capture_profile(session, tools.tools, task,
                                              out, reporter)

        try:
            return await asyncio.wait_for(body(), timeout=timeout)
        except asyncio.TimeoutError:
            out["error"] = f"任务超时({timeout:.0f}s)"
            reporter.log(out["error"], level="error")
            return out
        except Exception as e:
            out["error"] = mcp_runtime.describe_exc(e)
            reporter.log(f"MCP 会话异常: {out['error']}", level="error")
            return out
        finally:
            if tail:
                tail.close()
            if errlog:
                try:
                    errlog.close()
                except Exception:
                    pass
            if err_path:
                try:
                    os.unlink(err_path)
                except OSError:
                    pass
            if profile_dir:
                shutil.rmtree(profile_dir, ignore_errors=True)


class Handler(BaseHTTPRequestHandler):
    agent = None

    def log_message(self, *a):
        pass

    def _json(self, obj, code=200):
        b = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        if self.path == "/health":
            self._json({"ok": True, "agent_id": self.agent.agent_id,
                        "running": self.agent.running, "slots": self.agent.slots})
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self):
        if self.path != "/task":
            self._json({"error": "not found"}, 404)
            return
        n = int(self.headers.get("Content-Length", 0))
        task = json.loads(self.rfile.read(n).decode("utf-8") or "{}")
        ok, msg = self.agent.accept(task)
        self._json({"accepted": ok, "msg": msg}, 202 if ok else 429)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--platform", default="http://127.0.0.1:8000")
    ap.add_argument("--agent-id", default=f"agent-{socket.gethostname()}-1")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8001)
    ap.add_argument("--slots", type=int, default=2)
    ap.add_argument("--chromium", default=None)
    a = ap.parse_args()

    ag = Agent(a.platform, a.agent_id, a.host, a.port, a.slots, a.chromium)
    if not ag.register():
        log("无法连接平台，退出")
        sys.exit(1)
    threading.Thread(target=ag.heartbeat_loop, daemon=True).start()

    Handler.agent = ag
    srv = ThreadingHTTPServer((a.host, a.port), Handler)
    srv.daemon_threads = True
    log(f"执行机监听 http://{a.host}:{a.port}  容量={a.slots}")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        ag.stop_flag.set()
        ag.pool.shutdown(wait=False)
        srv.server_close()
        log("执行机已停止")


if __name__ == "__main__":
    main()
