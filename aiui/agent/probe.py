# -*- coding: utf-8 -*-
"""独立进程执行 MCP 能力探测。

worker 主循环不应被浏览器/Node 子进程的原生崩溃或卡死拖垮；本模块由
RemoteWorker 以子进程方式调用，只把探测结果写成 JSON 文件。
"""
import argparse
import json
import re
import sys
import time
from pathlib import Path

from aiui.common import mcp_runtime, profiles
from aiui.common.node_protocol import sanitize_mcp_capabilities
from aiui.common.paths import ROOT


def _mcp_version(mcp):
    for arg in mcp.get('args') or []:
        match = re.search(r'@[\w./-]+@([0-9][\w.\-]*)', str(arg))
        if match:
            return match.group(1)[:200]
    return ''


def _capability(mcp, tools, error):
    names = [t.get('name') for t in tools
             if isinstance(t, dict) and t.get('name')]
    cap = {
        'id': str(mcp.get('id') or ''),
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
    return cap


def probe(configs):
    results = []
    for mcp in configs:
        if not isinstance(mcp, dict) or not mcp.get('id'):
            continue
        tools, error = [], ''
        try:
            tools, error = mcp_runtime.probe_tools(mcp, ROOT, timeout=60)
        except BaseException as exc:
            error = f'{type(exc).__name__}: {exc}'
        results.append(_capability(mcp, tools, error))
    return sanitize_mcp_capabilities(results)


def main(argv=None):
    parser = argparse.ArgumentParser(description='MCP 能力探测子进程')
    parser.add_argument('--config', required=True)
    parser.add_argument('--output', required=True)
    args = parser.parse_args(argv)
    configs = json.loads(Path(args.config).read_text(encoding='utf-8'))
    if not isinstance(configs, list):
        print('探测配置必须是数组', file=sys.stderr)
        return 2
    result = probe(configs)
    Path(args.output).write_text(json.dumps(result, ensure_ascii=False), encoding='utf-8')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
