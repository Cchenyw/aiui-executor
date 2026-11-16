#requires -Version 5.1
<#
首次部署远程执行节点：
  1. 创建 app/runtime 分离布局
  2. 写入 remote-node.json 和 token（限制 ACL）
  3. 调用 update_remote_node.ps1 拉取代碼、安装依赖并自检
  4. 注册 Windows 计划任务并启动

当前 token 仍由平台侧 create_node_credentials.py 生成后单独传输；
后续可替换为一次性注册码（enrollment code）。
#>
param(
    [Parameter(Mandatory=$true)][string]$PlatformUrl,
    [Parameter(Mandatory=$true)][string]$NodeId,
    [Parameter(Mandatory=$true)][string]$MachineId,
    [Parameter(Mandatory=$true)][string]$TokenFile,
    [string]$Name = '',
    [int]$Slots = 1,
    [string[]]$McpIds = @('builtin-browser'),
    [string]$InstallDir = 'D:\AIUI',
    [string]$RepoUrl = 'https://github.com/Cchenyw/aiui-executor.git',
    [string]$Ref = 'latest',
    [ValidateSet('Release', 'Git')][string]$Mode = 'Release',
    [string[]]$SparsePaths = @(),
    [string]$PythonPath = '',
    [string]$PythonVersion = '',
    [string]$Wheelhouse = '',
    [string]$BrowsersPath = '',
    [string]$Chromium = '',
    [switch]$SkipBrowserInstall,
    [string]$CaFile = '',
    [string]$TaskName = 'AIUI Remote Node',
    [string]$TaskUser = 'SYSTEM',
    [string]$GitHubTokenEnv = 'AIUI_GITHUB_TOKEN',
    [switch]$AllowInsecureHttp,
    [switch]$NoStart,
    [switch]$SkipDeps,
    [switch]$SkipSelfCheck,
    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
$env:PYTHONIOENCODING = 'utf-8'
$env:PYTHONUTF8 = '1'

if ($SparsePaths.Count -eq 1 -and $SparsePaths[0] -match ',') {
    $SparsePaths = @($SparsePaths[0] -split ',')
}

$InstallDir = [System.IO.Path]::GetFullPath($InstallDir)
$AppDir = Join-Path $InstallDir 'app'
$RuntimeDir = Join-Path $InstallDir 'runtime'
if (-not $BrowsersPath) { $BrowsersPath = Join-Path $InstallDir 'browsers' }
$BrowsersPath = [System.IO.Path]::GetFullPath($BrowsersPath)
$configPath = Join-Path $RuntimeDir 'remote-node.json'
$tokenPath = Join-Path $RuntimeDir ('secrets\' + $NodeId + '.token')
$Name = $Name.Trim()
if (-not $Name) { $Name = $NodeId }

if ($NodeId -notmatch '^[A-Za-z0-9_.-]{1,64}$') { throw 'node_id 只能使用 1-64 个字母、数字、点、下划线或短横线' }
if ($MachineId -notmatch '^[A-Za-z0-9_.-]{1,64}$') { throw 'machine_id 只能使用 1-64 个字母、数字、点、下划线或短横线' }
if ($Slots -lt 1 -or $Slots -gt 128) { throw 'slots 必须在 1-128 之间' }
if (-not $McpIds -or $McpIds.Count -eq 0) { throw '至少需要一个 MCP ID' }

try {
    $uri = [Uri]$PlatformUrl
} catch {
    throw 'platform_url 不是合法 URL'
}
if ($uri.Scheme -notin @('http', 'https')) { throw 'platform_url 必须是 http/https' }
if ($uri.UserInfo -or $uri.Query -or $uri.Fragment -or $uri.AbsolutePath -notin @('/', '')) {
    throw 'platform_url 必须是不带用户信息、路径和查询参数的根地址'
}
if ($uri.Scheme -eq 'http' -and -not $uri.IsLoopback -and -not $AllowInsecureHttp) {
    throw '非回环 HTTP 必须显式加 -AllowInsecureHttp'
}

$TokenFile = [System.IO.Path]::GetFullPath($TokenFile)
if (-not (Test-Path -LiteralPath $TokenFile)) { throw ('token 文件不存在: ' + $TokenFile) }
$tokenBytes = [System.IO.File]::ReadAllBytes($TokenFile)
if ($tokenBytes.Length -ge 2 -and $tokenBytes[0] -eq 0xFF -and $tokenBytes[1] -eq 0xFE) {
    throw 'token 文件是 UTF-16 编码，请另存为 UTF-8/ANSI 文本'
}
$tokenText = (Get-Content -LiteralPath $TokenFile -Raw).Trim()
if (-not $tokenText) { throw 'token 文件为空' }
if ($tokenText -match '\s') { throw 'token 文件包含空白字符' }

if ($CaFile) {
    $CaFile = [System.IO.Path]::GetFullPath($CaFile)
    if (-not (Test-Path -LiteralPath $CaFile)) { throw ('CA 文件不存在: ' + $CaFile) }
}

$config = [ordered]@{
    platform_url        = $PlatformUrl
    node_id             = $NodeId
    machine_id          = $MachineId
    name                = $Name
    slots               = $Slots
    mcp_ids             = @($McpIds)
    state_dir           = 'remote-nodes/' + $NodeId
    token_file          = 'secrets/' + $NodeId + '.token'
    allow_insecure_http = [bool]$AllowInsecureHttp
}
if ($CaFile) { $config['ca_file'] = $CaFile }

if ($Chromium) {
    $Chromium = [System.IO.Path]::GetFullPath($Chromium)
    if (-not (Test-Path -LiteralPath $Chromium)) { throw ('Chromium 文件不存在: ' + $Chromium) }
    $config['chromium'] = $Chromium
    $SkipBrowserInstall = $true
}

if ($DryRun) {
    Write-Host '===== DryRun：安装计划 ====='
    Write-Host ('app: ' + $AppDir)
    Write-Host ('runtime: ' + $RuntimeDir)
    Write-Host ('browsers: ' + $BrowsersPath)
    Write-Host ('config: ' + $configPath)
    Write-Host ('token 目标: ' + $tokenPath)
    Write-Host ('任务: ' + $TaskName + '（用户 ' + $TaskUser + '）')
    Write-Host '配置内容:'
    $config | ConvertTo-Json -Depth 5 | Write-Host
    Write-Host ('更新命令: update_remote_node.ps1 -InstallDir "' + $InstallDir + '" -Mode ' + $Mode + ' -RepoUrl "' + $RepoUrl + '" -Ref "' + $Ref + '"')
    exit 0
}

New-Item -ItemType Directory -Force -Path $AppDir | Out-Null
New-Item -ItemType Directory -Force -Path $RuntimeDir | Out-Null
New-Item -ItemType Directory -Force -Path $BrowsersPath | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $RuntimeDir 'secrets') | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $RuntimeDir 'remote-nodes') | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $RuntimeDir 'logs') | Out-Null

Copy-Item -LiteralPath $TokenFile -Destination $tokenPath -Force
& icacls $tokenPath /inheritance:r /grant:r "${TaskUser}:(R)" "SYSTEM:(R)" "Administrators:(F)" | Out-Null
if ($LASTEXITCODE -ne 0) { Write-Warning '设置 token ACL 失败，请手动限制该文件权限。' }

$config | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $configPath -Encoding utf8
Write-Host ('已写入配置: ' + $configPath)
Write-Host ('已写入 token: ' + $tokenPath)

foreach ($p in @($RuntimeDir, $BrowsersPath, $AppDir, $configPath)) {
    if ($p -match "'") { throw ('路径中包含单引号，无法生成启动器: ' + $p) }
}
$launcher = Join-Path $RuntimeDir 'start-node.ps1'
$launcherText = @'
$ErrorActionPreference = 'Stop'
$env:AIUI_RUNTIME_DIR = '{0}'
$env:PLAYWRIGHT_BROWSERS_PATH = '{1}'
$env:PYTHONIOENCODING = 'utf-8'
$env:PYTHONUTF8 = '1'
$logDir = '{2}'
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
& '{3}' -Config '{4}' *>> '{5}'
exit $LASTEXITCODE
'@ -f $RuntimeDir, $BrowsersPath, (Join-Path $RuntimeDir 'logs'), (Join-Path $AppDir 'scripts/start_remote_node.ps1'), $configPath, (Join-Path $RuntimeDir 'logs/remote-node.log')
Set-Content -LiteralPath $launcher -Value $launcherText -Encoding utf8
Write-Host ('已写入启动器: ' + $launcher)

$updater = Join-Path $PSScriptRoot 'update_remote_node.ps1'
if (-not (Test-Path -LiteralPath $updater)) { throw ('缺少更新脚本: ' + $updater) }
$updateArgs = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $updater,
    '-InstallDir', $InstallDir, '-Mode', $Mode, '-RepoUrl', $RepoUrl, '-Ref', $Ref,
    '-TaskName', $TaskName, '-NoRestart', '-GitHubTokenEnv', $GitHubTokenEnv,
    '-BrowsersPath', $BrowsersPath)
if ($SparsePaths.Count -gt 0) { $updateArgs += @('-SparsePaths', ($SparsePaths -join ',')) }
if ($PythonPath) { $updateArgs += @('-PythonPath', $PythonPath) }
if ($PythonVersion) { $updateArgs += @('-PythonVersion', $PythonVersion) }
if ($Wheelhouse) { $updateArgs += @('-Wheelhouse', $Wheelhouse) }
if ($SkipDeps) { $updateArgs += '-SkipDeps' }
if ($SkipBrowserInstall) { $updateArgs += '-SkipBrowserInstall' }
if ($SkipSelfCheck) { $updateArgs += '-SkipSelfCheck' }
& powershell @updateArgs
if ($LASTEXITCODE -ne 0) { throw ('拉取/安装执行机代码失败（退出码 ' + $LASTEXITCODE + '）') }

$taskArg = '-NoProfile -ExecutionPolicy Bypass -File "' + $launcher + '"'
$action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument $taskArg -WorkingDirectory $AppDir
$trigger = New-ScheduledTaskTrigger -AtStartup
$trigger.Delay = 'PT30S'
$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 5) -ExecutionTimeLimit (New-TimeSpan -Days 0)
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings `
    -User $TaskUser -RunLevel Highest -Force | Out-Null
Write-Host ('已注册计划任务: ' + $TaskName)

if (-not $NoStart) {
    Start-ScheduledTask -TaskName $TaskName
    Write-Host '计划任务已启动'
} else {
    Write-Host ('已指定 -NoStart；手动启动: Start-ScheduledTask -TaskName "' + $TaskName + '"')
}

Write-Host ''
Write-Host '安装完成。下一步：'
Write-Host '  1. 在父平台 /agents 确认远程节点在线'
Write-Host '  2. 指定该节点运行一个 builtin-browser 脚本用例'
Write-Host '  3. 后续更新使用 update_remote_node.ps1（无需重新写入 token/config）'
Write-Host ('  日志: ' + (Join-Path $RuntimeDir 'logs\remote-node.log'))