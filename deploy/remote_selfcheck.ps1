#requires -Version 5.1
<#
远程节点自检脚本：检查配置 JSON、编码、必要字段、token 来源和网关连通性，
并把完整结果写入脚本目录下的 selfcheck-output\remote-selfcheck-<时间>.txt。

用法（在远程 Windows 主机上）：
  powershell -NoProfile -ExecutionPolicy Bypass -File scripts\remote_selfcheck.ps1
  powershell -NoProfile -ExecutionPolicy Bypass -File scripts\remote_selfcheck.ps1 -SkipGatewayCheck
  powershell -NoProfile -ExecutionPolicy Bypass -File scripts\remote_selfcheck.ps1 `
      -ProjectRoot D:\ui_test\AIUI_test_v4 -ConfigPath D:\ui_test\AIUI_test_v4\runtime\remote-node.json

注意：脚本不会打印 token 原文，只输出文件头、长度和格式检查结果。
#>
param(
    [string]$ProjectRoot = 'D:\ui_test\AIUI_test_v4',
    [string]$ConfigPath = '',
    [string]$OutputDir = '',
    [switch]$SkipGatewayCheck
)

$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
$env:PYTHONIOENCODING = 'utf-8'
$env:PYTHONUTF8 = '1'

if (-not $ConfigPath) {
    $ConfigPath = Join-Path $ProjectRoot 'runtime\remote-node.json'
}
if (-not [System.IO.Path]::IsPathRooted($ConfigPath)) {
    $ConfigPath = Join-Path $ProjectRoot $ConfigPath
}
if (-not $OutputDir) {
    $OutputDir = Join-Path $PSScriptRoot 'selfcheck-output'
}
New-Item -ItemType Directory -Force -Path $OutputDir | Out-Null
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$outFile = Join-Path $OutputDir "remote-selfcheck-$stamp.txt"

$lines = New-Object System.Collections.Generic.List[string]
function Add-Line([string]$text = '') {
    $lines.Add($text)
}

Add-Line '===== AIUI 远程节点自检 ====='
Add-Line ("时间: " + (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'))
Add-Line ("主机: " + $env:COMPUTERNAME + "  用户: " + $env:USERNAME)
Add-Line ("项目目录: " + $ProjectRoot)
Add-Line ("配置文件: " + $ConfigPath)
Add-Line ("输出文件: " + $outFile)
Add-Line ''

$exitCode = 0

if (-not (Test-Path -LiteralPath $ProjectRoot)) {
    Add-Line ("[FAIL] 项目目录不存在: " + $ProjectRoot)
    $exitCode = 2
} else {
    $python = Join-Path $ProjectRoot '.venv\Scripts\python.exe'
    if (-not (Test-Path -LiteralPath $python)) {
        Add-Line ("[FAIL] 虚拟环境不存在: " + $python)
        Add-Line '请先按部署步骤创建 .venv 并安装 requirements.txt。'
        $exitCode = 2
    } else {
        Add-Line ("Python: " + ((& $python --version 2>&1) -join ' '))
        Add-Line ''

        $diagnose = @'
import os
import socket
import sys
import traceback
from pathlib import Path
from urllib.parse import urlsplit

if len(sys.argv) < 3:
    print('[FAIL] 参数不足：需要配置路径和项目目录')
    sys.exit(2)

config_path = Path(sys.argv[1]).expanduser().resolve()
project_root = str(Path(sys.argv[2]).expanduser().resolve())
if project_root not in sys.path:
    sys.path.insert(0, project_root)

failures = []


def report(name, ok, detail=''):
    text = ('[PASS] ' if ok else '[FAIL] ') + name
    if detail != '':
        text += ': ' + str(detail)
    print(text)
    if not ok:
        failures.append(name)


def safe(name, fn):
    try:
        value = fn()
        report(name, True, value)
        return value
    except Exception as exc:
        report(name, False, f'{type(exc).__name__}: {exc}')
        return None


print('Python:', sys.version.split()[0])
print('可执行文件:', sys.executable)
print('项目目录:', project_root)
print('配置文件:', config_path)
print()

report('配置文件存在', config_path.is_file(), config_path)
if not config_path.is_file():
    print('\n结论: 配置文件不存在，自检终止')
    sys.exit(2)

header = safe('配置文件可读（文件头）', lambda: config_path.read_bytes()[:4].hex())
if header and header.startswith('fffe'):
    report('配置文件编码', False, 'UTF-16LE，需另存为 UTF-8')

try:
    from aiui.agent.remote import load_config, GatewayClient
except Exception:
    print('[FAIL] 导入 aiui.agent.remote 失败:')
    traceback.print_exc()
    print('\n结论: 项目依赖或 Python 环境不完整')
    sys.exit(2)

try:
    config = load_config(config_path)
    report('配置文件 JSON 解析', True, '字段: ' + ', '.join(sorted(config)))
except Exception as exc:
    report('配置文件 JSON 解析', False, f'{type(exc).__name__}: {exc}')
    config = None
if not isinstance(config, dict):
    print('\n结论: 配置文件无法解析，先修复编码/JSON 格式')
    sys.exit(2)

for key in ('node_id', 'machine_id', 'name', 'state_dir', 'platform_url'):
    value = config.get(key)
    ok = isinstance(value, str) and bool(value.strip())
    detail = repr(value) if isinstance(value, str) else 'type=' + type(value).__name__
    report('必要字段 ' + key, ok, detail)

slots = config.get('slots', 1)
report('slots 是 1-128 的整数', type(slots) is int and 1 <= slots <= 128, repr(slots))

mcp_ids = config.get('mcp_ids')
report('mcp_ids 是非空字符串数组',
       isinstance(mcp_ids, list) and bool(mcp_ids)
       and all(isinstance(x, str) and x for x in mcp_ids), repr(mcp_ids))

has_file = bool(config.get('token_file'))
has_env = bool(config.get('token_env'))
report('token 来源唯一', has_file != has_env,
       'token_file' if has_file and not has_env
       else ('token_env' if has_env and not has_file else '未配置或同时配置'))

token = ''
if has_file and not has_env:
    token_path = Path(config['token_file'])
    report('token 文件存在', token_path.is_file(), str(token_path))
    if token_path.is_file():
        try:
            head = token_path.read_bytes()[:2]
            if head == bytes((255, 254)):
                report('token 文件编码', False, 'UTF-16LE，需另存为 UTF-8')
            else:
                report('token 文件编码', True, 'ok')
        except Exception as exc:
            report('token 文件编码', False, f'{type(exc).__name__}: {exc}')
        try:
            token = token_path.read_text(encoding='utf-8-sig').strip()
            report('读取 token 文件', True, '长度=%d' % len(token))
        except Exception as exc:
            report('读取 token 文件', False, f'{type(exc).__name__}: {exc}')
            token = ''
elif has_env and not has_file:
    token = os.environ.get(config.get('token_env') or '', '').strip()
    report('token 环境变量非空', bool(token), config.get('token_env') or '')

report('token 非空', bool(token), '长度=%d' % len(token))
has_space = any(ch.isspace() for ch in token)
report('token 不含空白字符', bool(token) and not has_space,
       'ok' if token and not has_space else '为空或含空白')

url = str(config.get('platform_url') or '')
parts = urlsplit(url)
host = parts.hostname or ''
loopback = host in ('localhost', '::1') or host.startswith('127.')
if parts.scheme == 'http' and host and not loopback:
    report('非回环 HTTP 已显式允许', config.get('allow_insecure_http') is True,
           repr(config.get('allow_insecure_http')))


def tcp_check():
    port = parts.port or (443 if parts.scheme == 'https' else 80)
    with socket.create_connection((host, port), timeout=3):
        return '%s:%s' % (host, port)


if host:
    safe('网关 TCP 连通性', tcp_check)


def build_client():
    GatewayClient(url, str(config.get('node_id') or ''), token,
                  config.get('ca_file'), config.get('allow_insecure_http', False))
    return 'ok'


safe('配置与凭证格式（GatewayClient）', build_client)

print()
if failures:
    print('结论: %d 项未通过 -> %s' % (len(failures), '；'.join(failures)))
    sys.exit(2)
print('结论: 配置与凭证诊断全部通过')
sys.exit(0)
'@

        $diagPath = Join-Path ([System.IO.Path]::GetTempPath()) ('aiui-selfcheck-' + [guid]::NewGuid().ToString('N') + '.py')
        Set-Content -LiteralPath $diagPath -Value $diagnose -Encoding utf8
        try {
            Add-Line '===== 配置与凭证诊断 ====='
            $diagOut = & $python -X utf8 $diagPath $ConfigPath $ProjectRoot 2>&1 | Out-String
            $diagCode = $LASTEXITCODE
            Add-Line ([string]$diagOut).TrimEnd()
            Add-Line ("诊断退出码: " + $diagCode)
            if ($diagCode -ne 0) { $exitCode = 2 }
        } finally {
            Remove-Item -LiteralPath $diagPath -Force -ErrorAction SilentlyContinue
        }

        if (-not $SkipGatewayCheck) {
            Add-Line ''
            Add-Line '===== 官方自检（注册 / 网络 / 凭证） ====='
            $nodeScript = Join-Path $ProjectRoot 'scripts\start_remote_node.ps1'
            if (Test-Path -LiteralPath $nodeScript) {
                $checkOut = & powershell -NoProfile -ExecutionPolicy Bypass -File $nodeScript -Config $ConfigPath -Check 2>&1 | Out-String
                $checkCode = $LASTEXITCODE
                Add-Line ([string]$checkOut).TrimEnd()
                Add-Line ("官方自检退出码: " + $checkCode)
                if ($checkCode -ne 0) { $exitCode = 2 }
            } else {
                Add-Line ("[FAIL] 未找到: " + $nodeScript)
                $exitCode = 2
            }
        }
    }
}

Add-Line ''
if ($exitCode -eq 0) {
    Add-Line '===== 结论: 自检通过 ====='
} else {
    Add-Line '===== 结论: 自检未通过，请查看上方 [FAIL] 项 ====='
}

$lines | Set-Content -LiteralPath $outFile -Encoding utf8
Write-Host ($lines -join [Environment]::NewLine)
Write-Host ''
Write-Host ('完整结果已写入: ' + $outFile)
exit $exitCode
