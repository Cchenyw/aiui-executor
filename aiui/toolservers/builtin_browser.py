# -*- coding: utf-8 -*-
"""
MCP 浏览器工具服务（stdio 传输，真 JSON-RPC）

━━━ 关键决策与选型原因 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

【决策 1】为什么用 MCP 协议，而不是 Agent 里直接调 Playwright API？
  · 原因：工具层是"契约"，不是实现。Agent 只认识 navigate/click/expect_text 这组语义，
    底层换 Playwright、Selenium、还是自研驱动，Agent 一行不用改。
  · 附加收益：每次 call_tool 天然是审计粒度（谁、何时、调了什么、结果如何），
    正是平台化要的 trace；直接调 API 就没有这层边界。
  · 代价：多一层进程与序列化开销（本 demo 单任务约 +0.3s，可接受）。

【决策 2】为什么选 stdio 传输，不用 HTTP/SSE 的 MCP？
  · 进程生命周期 = 会话生命周期：client 断开，server 必然退出，不存在"忘了关"的残留。
  · 不需要分配端口，N 个任务并行不会抢端口，也不需要做鉴权。
  · 代价：只能同机部署。跨机场景才需要 streamable-http，本 demo 不需要。

【决策 3】为什么每个任务起一个独立 server 子进程 + 独立浏览器？
  · 会话隔离是测试红线：cookie、storage、缓存、弹窗状态互不污染，用例之间零串扰。
  · 失败用例最脏（残留未完成的 wait、卡住的弹窗），进程级隔离能保证"杀掉即干净"。
  · 代价：进程启动开销（约 0.5~1s/任务），换来的是可并行、可强杀。

【决策 4】为什么必须用 async playwright？
  · MCP 的 call_tool 是 async 函数，运行在 asyncio 事件循环里。
  · 在事件循环中调用 sync API 会直接抛：
    "It looks like you are using Playwright Sync API inside the asyncio loop"。
  · 这不是风格问题，是硬约束。

【决策 5】为什么日志一律写 stderr，绝不 print 到 stdout？
  · stdio 传输时，stdout 就是 JSON-RPC 协议通道。
  · 任何一行普通日志混进 stdout，客户端立刻报 "Failed to parse JSONRPC message"，
    表现为"工具莫名其妙不可用"，极难排查。这是本类项目最高频的事故。

【决策 6】为什么工具出错也返回 {"error": ...} 字符串，而不是 raise？
  · 客户端（agent_runner）靠解析返回文本识别失败。FastMCP/MCPServer 会把 raise
    转成 isError 的裸文本，错误契约就破了，客户端只能靠猜。
  · 统一契约：成功 {"ok": true, ...}，失败 {"error": "..."}，断言 {"ok": bool, "actual": "..."}。

【决策 7】为什么用 DOM 文本/选择器，而不是视觉模型点坐标？
  · 成本：一次截图 + 视觉推理是纯文本的几十倍。
  · 稳定性：分辨率、主题、字体变化都会让坐标漂移；选择器与文本不漂。
  · 可审计：失败时能说清"没找到 #submit"，而不是"我点了 (312,486) 但没反应"。
  · 代价：选择器/文案一改就得维护，所以真实项目要配 accessibility 快照兜底。

【决策 8】浏览器路径为什么自动探测，不写死？
  · 跨机交付会崩：Linux 的 chrome-linux64 和 Windows 的 chrome.exe 路径完全不同，
    写死路径的脚本在别人机器上必然 "Executable doesn't exist"。

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""

import argparse
import asyncio
import base64
import glob
import json
import os
import sys

# ── 版本兼容 shim ─────────────────────────────────────────────────────
# mcp ≤1.26 用 FastMCP；1.27+ 移除低层装饰器；2.x 把 FastMCP 改名 MCPServer。
# 修版本漂移只需要动这一处，业务代码完全不变。
try:                                                    # mcp ≤ 1.x
    from mcp.server.fastmcp import FastMCP as MCPServer
    SDK_FLAVOR = "fastmcp(1.x)"
except Exception:                                       # mcp 2.x
    from mcp.server.mcpserver import MCPServer
    SDK_FLAVOR = "mcpserver(2.x)"

from playwright.async_api import async_playwright


def log(*a):
    """所有日志走 stderr：stdout 是协议通道（见决策 5）"""
    print("[mcp]", *a, file=sys.stderr, flush=True)


def find_chromium(explicit=None):
    """自动探测浏览器：显式参数 > 环境变量 > playwright 缓存（见决策 8）"""
    if explicit and os.path.exists(explicit):
        return explicit
    env = os.environ.get("CHROMIUM_EXE")
    if env and os.path.exists(env):
        return env
    pats = [
        os.path.expanduser("~/.cache/ms-playwright/chromium-*/chrome-linux64/chrome"),
        os.path.expanduser("~/.cache/ms-playwright/chromium-*/chrome-linux/chrome"),
        os.path.expanduser("~/Library/Caches/ms-playwright/chromium-*/chrome-mac/Chromium.app/Contents/MacOS/Chromium"),
        os.path.expanduser("~/AppData/Local/ms-playwright/chromium-*/chrome-win/chrome.exe"),
    ]
    for p in pats:
        hits = sorted(glob.glob(p))
        if hits:
            return hits[-1]
    return None                                          # None = 交给 playwright 自己找


STATE = {"page": None, "context": None, "browser": None}
app = MCPServer("browser-tools")
TIMEOUT = 8000


@app.tool()
async def navigate(url: str) -> str:
    """打开页面并等待 DOM 就绪，返回标题与最终地址。"""
    try:
        page = STATE["page"]
        await page.goto(url, wait_until="domcontentloaded", timeout=TIMEOUT)
        return json.dumps({"ok": True, "url": page.url, "title": await page.title()}, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"error": f"navigate 失败: {type(e).__name__}: {e}"}, ensure_ascii=False)


@app.tool()
async def type_text(selector: str, text: str) -> str:
    """向输入框写入文本（fill 会先清空，比逐字符键入更稳）。"""
    try:
        await STATE["page"].fill(selector, text, timeout=TIMEOUT)
        return json.dumps({"ok": True, "selector": selector, "value": text}, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"error": f"type_text 失败({selector}): {type(e).__name__}: {e}"}, ensure_ascii=False)


@app.tool()
async def click(selector: str) -> str:
    """点击元素，返回点击后的地址（便于判断是否发生跳转）。"""
    try:
        await STATE["page"].click(selector, timeout=TIMEOUT)
        return json.dumps({"ok": True, "selector": selector, "url": STATE["page"].url}, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"error": f"click 失败({selector}): {type(e).__name__}: {e}"}, ensure_ascii=False)


@app.tool()
async def expect_text(text: str, timeout_ms: int = 5000) -> str:
    """断言页面正文包含指定文本。

    为什么不直接用 wait_for_selector(text=...)：文本节点常被拆进多个 span，
    选择器匹配会误判失败。轮询 body.innerText 更贴近"用户看到什么"。
    """
    try:
        page = STATE["page"]
        deadline = asyncio.get_event_loop().time() + timeout_ms / 1000.0
        while True:
            body = await page.inner_text("body")
            if text in body:
                return json.dumps({"ok": True, "actual": text}, ensure_ascii=False)
            if asyncio.get_event_loop().time() > deadline:
                return json.dumps({"ok": False, "actual": body.strip()[:200]}, ensure_ascii=False)
            await asyncio.sleep(0.15)
    except Exception as e:
        return json.dumps({"error": f"expect_text 失败: {type(e).__name__}: {e}"}, ensure_ascii=False)


@app.tool()
async def page_text() -> str:
    """返回页面可见正文（截断 800 字），用于失败归因与审计留痕。"""
    try:
        body = await STATE["page"].inner_text("body")
        return json.dumps({"ok": True, "text": body.strip()[:800]}, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"error": f"page_text 失败: {type(e).__name__}: {e}"}, ensure_ascii=False)


SNAPSHOT_JS = r"""
() => {
  const visible = (el) => {
    const r = el.getBoundingClientRect();
    const s = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && s.display !== 'none' && s.visibility !== 'hidden';
  };
  const cssEsc = (s) => (window.CSS && CSS.escape) ? CSS.escape(String(s))
    : String(s).replace(/["\\]/g, '\\$&');
  const selectorFor = (el) => {
    if (el.id) return '#' + cssEsc(el.id);
    const tid = el.getAttribute('data-testid');
    if (tid) return `[data-testid="${tid}"]`;
    if (el.getAttribute('name')) return `${el.tagName.toLowerCase()}[name="${el.getAttribute('name')}"]`;
    const parts = [];
    let cur = el;
    while (cur && cur.nodeType === 1 && parts.length < 5) {
      let part = cur.tagName.toLowerCase();
      if (cur.classList && cur.classList.length) part += '.' + cssEsc(cur.classList[0]);
      const parent = cur.parentElement;
      if (parent) {
        const same = Array.from(parent.children).filter(x => x.tagName === cur.tagName);
        if (same.length > 1) part += `:nth-of-type(${same.indexOf(cur) + 1})`;
      }
      parts.unshift(part);
      cur = parent;
    }
    return parts.join(' > ');
  };
  const labelFor = (el) => {
    if (el.id) {
      const l = document.querySelector(`label[for="${cssEsc(el.id)}"]`);
      if (l) return (l.innerText || '').trim();
    }
    const p = el.closest('label');
    return p ? (p.innerText || '').trim() : '';
  };
  const elements = Array.from(document.querySelectorAll(
      'input,textarea,select,button,a,[role="button"],[contenteditable="true"]'))
    .filter(visible).slice(0, 120).map(el => ({
      tag: el.tagName.toLowerCase(),
      type: el.getAttribute('type') || '',
      id: el.id || '',
      name: el.getAttribute('name') || '',
      text: (el.innerText || el.getAttribute('aria-label') || el.getAttribute('placeholder') || '')
        .trim().slice(0, 120),
      placeholder: el.getAttribute('placeholder') || '',
      aria_label: el.getAttribute('aria-label') || '',
      label: labelFor(el).slice(0, 120),
      selector: selectorFor(el),
    }));
  return {
    url: location.href,
    title: document.title,
    text: (document.body ? document.body.innerText : '').slice(0, 5000),
    elements: elements,
  };
}
"""


@app.tool()
async def snapshot() -> str:
    """观察页面：返回可见正文 + 可交互元素的标签/选择器，供 AI 规划器决定下一步。"""
    try:
        data = await STATE["page"].evaluate(SNAPSHOT_JS)
        return json.dumps({"ok": True, **data}, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"error": f"snapshot 失败: {type(e).__name__}: {e}"}, ensure_ascii=False)


@app.tool()
async def screenshot() -> str:
    """截取当前视口，返回 PNG 图片内容，供执行平台保存每一步的现场。"""
    try:
        data = await STATE['page'].screenshot(type='png', full_page=False, timeout=3000)
        return json.dumps({'ok': True, 'mime_type': 'image/png',
                           'data': base64.b64encode(data).decode('ascii')})
    except Exception as e:
        return json.dumps({'error': f'screenshot 失败: {type(e).__name__}: {e}'}, ensure_ascii=False)


@app.tool()
async def profile_capture() -> str:
    """内部工具：采集当前上下文的 storage state（cookies + localStorage）。

    返回内容含敏感凭证，仅供执行机采集环境 Profile 使用；不要写入普通步骤日志，
    也不要暴露给 AI 规划器。
    """
    try:
        state = await STATE["context"].storage_state()
        return json.dumps({"ok": True, "storage_state": state}, ensure_ascii=False)
    except Exception as e:
        return json.dumps(
            {"error": f"profile_capture 失败: {type(e).__name__}: {e}"},
            ensure_ascii=False)


async def main(exe=None):
    chromium = find_chromium(exe)
    log("SDK:", SDK_FLAVOR, "| chromium:", chromium or "playwright 默认")
    state_path = os.environ.get("AIUI_PROFILE_STATE") or ""
    storage_state = None
    if state_path:
        if not os.path.exists(state_path):
            raise FileNotFoundError(f"AIUI_PROFILE_STATE 不存在: {state_path}")
        with open(state_path, encoding="utf-8") as f:
            storage_state = json.load(f)
        log("已加载执行环境 Profile:", state_path)
    async with async_playwright() as pw:
        kw = {"headless": True, "args": ["--no-sandbox", "--disable-dev-shm-usage"]}
        if chromium:
            kw["executable_path"] = chromium
        browser = await pw.chromium.launch(**kw)
        ctx = await browser.new_context(
            viewport={"width": 1280, "height": 900}, storage_state=storage_state)
        page = await ctx.new_page()
        STATE["browser"], STATE["context"], STATE["page"] = browser, ctx, page
        try:
            # run_stdio_async 在 client 断开后返回；随后做显式清理，
            # 不用 atexit（无法 await），避免 Windows 上留孤儿 Chromium。
            await app.run_stdio_async()
        finally:
            try:
                await ctx.close()
            finally:
                await browser.close()
            log("已清理浏览器")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--chromium", default=None)
    a = ap.parse_args()
    try:
        asyncio.run(asyncio.wait_for(main(a.chromium), timeout=None))
    except KeyboardInterrupt:
        pass
