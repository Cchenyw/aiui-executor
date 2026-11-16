param(
    [Parameter(Mandatory=$true)][string]$Config,
    [string]$RuntimeDir = '',
    [string]$BrowsersPath = '',
    [string]$LogFile = '',
    [switch]$Check
)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $projectRoot
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
$env:PYTHONIOENCODING = 'utf-8'
$env:PYTHONUTF8 = '1'
if ($RuntimeDir) {
    $RuntimeDir = [System.IO.Path]::GetFullPath($RuntimeDir)
    if (-not (Test-Path -LiteralPath $RuntimeDir)) {
        throw "运行目录不存在: $RuntimeDir"
    }
    $env:AIUI_RUNTIME_DIR = $RuntimeDir
}
if ($BrowsersPath) {
    $BrowsersPath = [System.IO.Path]::GetFullPath($BrowsersPath)
    $env:PLAYWRIGHT_BROWSERS_PATH = $BrowsersPath
}
$pythonExe = Join-Path $projectRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $pythonExe)) {
    throw '缺少项目虚拟环境，请先安装 Python 并创建 .venv。'
}
$nodeArgs = @('-B', '-m', 'aiui.agent.remote', '--config', $Config)
if ($Check) { $nodeArgs += '--check' }
if ($LogFile) {
    $logDir = Split-Path -Parent $LogFile
    if ($logDir) { New-Item -ItemType Directory -Force -Path $logDir | Out-Null }
    & $pythonExe @nodeArgs 2>&1 | Tee-Object -FilePath $LogFile -Append
    exit $LASTEXITCODE
}
& $pythonExe @nodeArgs
exit $LASTEXITCODE
