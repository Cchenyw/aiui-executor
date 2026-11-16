#requires -Version 5.1
<#
把现有执行机项目目录迁移为“代码/运行数据分离”布局：
  D:\AIUI\app      代码（由 update_remote_node.ps1 更新）
  D:\AIUI\runtime  remote-node.json、secrets、remote-nodes、logs

迁移是复制而不是移动，源目录保留作为备份。迁移前请先停止正在运行的节点窗口；
脚本检测到节点进程会拒绝执行，除非显式加 -ForceStop。
#>
param(
    [string]$SourceDir = 'D:\ui_test\AIUI_test_v4',
    [string]$InstallDir = 'D:\AIUI',
    [string]$PythonPath = '',
    [string]$PythonVersion = '',
    [string]$BrowsersPath = '',
    [string]$Chromium = '',
    [switch]$SkipBrowserInstall,
    [string]$TaskName = 'AIUI Remote Node',
    [string]$TaskUser = 'SYSTEM',
    [switch]$RegisterTask,
    [switch]$NoStart,
    [switch]$SkipDeps,
    [switch]$SkipSelfCheck,
    [switch]$ForceStop,
    [switch]$DryRun,
    [switch]$Force
)

$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
$env:PYTHONIOENCODING = 'utf-8'
$env:PYTHONUTF8 = '1'

$SourceDir = [System.IO.Path]::GetFullPath($SourceDir)
$InstallDir = [System.IO.Path]::GetFullPath($InstallDir)
$AppDir = Join-Path $InstallDir 'app'
$RuntimeDir = Join-Path $InstallDir 'runtime'
if (-not $BrowsersPath) { $BrowsersPath = Join-Path $InstallDir 'browsers' }
$BrowsersPath = [System.IO.Path]::GetFullPath($BrowsersPath)
$configPath = Join-Path $RuntimeDir 'remote-node.json'
$logDir = Join-Path $RuntimeDir 'logs'
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$logFile = Join-Path $logDir ('migrate-' + $stamp + '.log')

function Write-Log([string]$message) {
    $line = '[' + (Get-Date -Format 'HH:mm:ss') + '] ' + $message
    Write-Host $line
    if (-not $DryRun) {
        New-Item -ItemType Directory -Force -Path $logDir | Out-Null
        Add-Content -LiteralPath $logFile -Value $line -Encoding utf8
    }
}

function Get-WorkerProcesses {
    @(Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='pythonw.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -and $_.CommandLine -match 'aiui\.agent\.remote' })
}

Write-Log '===== AIUI 执行机目录迁移 ====='
Write-Log ('源目录: ' + $SourceDir)
Write-Log ('目标: app=' + $AppDir + ' | runtime=' + $RuntimeDir)
Write-Log ('Browsers: ' + $BrowsersPath)

if (-not (Test-Path -LiteralPath (Join-Path $SourceDir 'runtime\remote-node.json'))) {
    throw ('源目录缺少 runtime\remote-node.json: ' + $SourceDir)
}
if (-not (Test-Path -LiteralPath (Join-Path $SourceDir 'scripts\start_remote_node.ps1'))) {
    throw ('源目录缺少 scripts\start_remote_node.ps1: ' + $SourceDir)
}
if ((Test-Path -LiteralPath $configPath) -and -not $Force) {
    throw ('目标 runtime 已存在，不会覆盖: ' + $configPath + '。加 -Force 可继续覆盖/合并，或先备份目标目录。')
}

$procs = @(Get-WorkerProcesses)
if ($procs.Count -gt 0) {
    $pids = (($procs | ForEach-Object { $_.ProcessId }) -join ', ')
    if (-not $ForceStop) {
        throw ('检测到正在运行的远程节点进程（PID: ' + $pids + '）。请先停止节点窗口，或使用 -ForceStop。')
    }
    Write-Log ('强制结束节点进程: ' + $pids)
    $procs | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
    Start-Sleep -Seconds 2
    if (Get-WorkerProcesses) { throw '强制结束后仍有节点进程在运行' }
}

if ($DryRun) {
    Write-Log 'DryRun：将执行以下步骤（不实际复制/安装/注册）：'
    Write-Log ('  1. robocopy ' + $SourceDir + ' -> ' + $AppDir + '（排除 .venv/runtime/artifacts/__pycache__/.git）')
    Write-Log ('  2. robocopy ' + (Join-Path $SourceDir 'runtime') + ' -> ' + $RuntimeDir)
    Write-Log ('  3. 在 ' + $AppDir + ' 创建 .venv 并安装 requirements.txt')
    Write-Log ('  4. 安装 Chromium 到 ' + $BrowsersPath)
    Write-Log '  5. 运行 remote_selfcheck.ps1（配置诊断）'
    if ($RegisterTask) {
        Write-Log ('  6. 注册计划任务 ' + $TaskName + '（用户 ' + $TaskUser + '）')
    }
    exit 0
}

New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null
New-Item -ItemType Directory -Force -Path $AppDir | Out-Null
New-Item -ItemType Directory -Force -Path $RuntimeDir | Out-Null

Write-Log '复制代码（排除 .venv/runtime/artifacts/__pycache__/.git）'
$rc = Start-Process -FilePath 'robocopy.exe' -Wait -PassThru -NoNewWindow -ArgumentList @(
    $SourceDir, $AppDir, '/E', '/XD', '.venv', 'runtime', 'artifacts', '__pycache__', '.git',
    '/XF', 'NUL', '/NFL', '/NDL', '/NJH', '/NJS', '/NP'
)
if ($rc.ExitCode -ge 8) { throw ('robocopy 复制代码失败，退出码 ' + $rc.ExitCode) }

Write-Log '复制运行数据（runtime）'
$rc = Start-Process -FilePath 'robocopy.exe' -Wait -PassThru -NoNewWindow -ArgumentList @(
    (Join-Path $SourceDir 'runtime'), $RuntimeDir, '/E', '/XD', '__pycache__',
    '/NFL', '/NDL', '/NJH', '/NJS', '/NP'
)
if ($rc.ExitCode -ge 8) { throw ('robocopy 复制 runtime 失败，退出码 ' + $rc.ExitCode) }

if (-not (Test-Path -LiteralPath $configPath)) {
    throw ('迁移后缺少配置文件: ' + $configPath)
}

if ($Chromium) {
    $Chromium = [System.IO.Path]::GetFullPath($Chromium)
    if (-not (Test-Path -LiteralPath $Chromium)) { throw ('Chromium 文件不存在: ' + $Chromium) }
    $cfg = Get-Content -LiteralPath $configPath -Raw | ConvertFrom-Json
    $cfg | Add-Member -NotePropertyName chromium -NotePropertyValue $Chromium -Force
    $cfg | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $configPath -Encoding utf8
    $SkipBrowserInstall = $true
    Write-Log ('已配置系统 Chromium: ' + $Chromium)
}
if ($SkipBrowserInstall -and -not $Chromium) {
    Write-Log '警告: 已跳过 Playwright Chromium 安装且未指定 -Chromium；请确认节点能启动浏览器。'
}

Get-ChildItem -LiteralPath (Join-Path $RuntimeDir 'secrets') -Filter '*.token' -ErrorAction SilentlyContinue | ForEach-Object {
    & icacls $_.FullName /inheritance:r /grant:r "${TaskUser}:(R)" "SYSTEM:(R)" "Administrators:(F)" | Out-Null
    if ($LASTEXITCODE -ne 0) { Write-Log ('设置 token ACL 失败，请手动限制: ' + $_.FullName) }
}

if (-not $SkipDeps) {
    $pythonExe = $PythonPath
    $pythonArgs = @()
    if (-not $pythonExe) {
        if (Get-Command py -ErrorAction SilentlyContinue) {
            $pythonExe = 'py'
            if ($PythonVersion) {
                $pythonArgs = @('-' + $PythonVersion)
            } else {
                $pythonArgs = @('-3')
            }
        } elseif (Get-Command python -ErrorAction SilentlyContinue) {
            $pythonExe = 'python'
        } else {
            throw '未找到 Python；请通过 -PythonPath 或 -PythonVersion 指定。'
        }
    }
    & $pythonExe @pythonArgs -c "import sys, struct; sys.exit(0 if sys.version_info >= (3, 10) and struct.calcsize('P') == 8 else 1)"
    if ($LASTEXITCODE -ne 0) {
        throw '需要 64 位 Python 3.10 或更高版本；可用 -PythonVersion 3.13 或 -PythonPath 指定。'
    }
    $pythonVersionText = (& $pythonExe @pythonArgs -c "import sys; print('.'.join(map(str, sys.version_info[:3])))").Trim()
    Write-Log ('使用 Python ' + $pythonVersionText + ' (' + $pythonExe + ' ' + ($pythonArgs -join ' ') + ')')
    $venvDir = Join-Path $AppDir '.venv'
    if (Test-Path -LiteralPath $venvDir) { Remove-Item -LiteralPath $venvDir -Recurse -Force }
    Write-Log '创建虚拟环境'
    & $pythonExe @pythonArgs -m venv $venvDir
    if ($LASTEXITCODE -ne 0) { throw '创建虚拟环境失败' }
    $venvPython = Join-Path $venvDir 'Scripts\python.exe'
    & $venvPython -m ensurepip --upgrade
    & $venvPython -m pip install --upgrade pip setuptools wheel
    Write-Log '安装依赖'
    & $venvPython -m pip install -r (Join-Path $AppDir 'requirements.txt')
    if ($LASTEXITCODE -ne 0) { throw '安装依赖失败' }
    & $venvPython -m pip check
    if ($LASTEXITCODE -ne 0) { throw 'pip check 失败' }
    if (-not $SkipBrowserInstall) {
        New-Item -ItemType Directory -Force -Path $BrowsersPath | Out-Null
        $env:PLAYWRIGHT_BROWSERS_PATH = $BrowsersPath
        Write-Log '安装 Playwright Chromium'
        & $venvPython -m playwright install chromium
        if ($LASTEXITCODE -ne 0) { throw 'Playwright Chromium 安装失败' }
    } else {
        Write-Log '已跳过 Playwright Chromium 安装'
    }
}

if (-not $SkipSelfCheck) {
    $selfCheck = Join-Path $AppDir 'scripts/remote_selfcheck.ps1'
    if (-not (Test-Path -LiteralPath $selfCheck)) { $selfCheck = Join-Path $PSScriptRoot 'remote_selfcheck.ps1' }
    if (-not (Test-Path -LiteralPath $selfCheck)) { throw ('缺少自检脚本: ' + $selfCheck) }
    Write-Log '运行配置自检'
    & powershell -NoProfile -ExecutionPolicy Bypass -File $selfCheck `
        -ProjectRoot $AppDir -ConfigPath $configPath `
        -OutputDir (Join-Path $logDir ('selfcheck-migrate-' + $stamp)) -SkipGatewayCheck
    if ($LASTEXITCODE -ne 0) { throw ('迁移后自检失败（退出码 ' + $LASTEXITCODE + '）') }
}

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
'@ -f $RuntimeDir, $BrowsersPath, $logDir, (Join-Path $AppDir 'scripts/start_remote_node.ps1'), $configPath, (Join-Path $logDir 'remote-node.log')
Set-Content -LiteralPath $launcher -Value $launcherText -Encoding utf8
Write-Log ('已写入启动器: ' + $launcher)

if ($RegisterTask) {
    $arg = '-NoProfile -ExecutionPolicy Bypass -File "' + $launcher + '"'
    $action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument $arg -WorkingDirectory $AppDir
    $trigger = New-ScheduledTaskTrigger -AtStartup
    $settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -RestartCount 3 `
        -RestartInterval (New-TimeSpan -Minutes 5) -ExecutionTimeLimit (New-TimeSpan -Days 0)
    Write-Log ('注册计划任务: ' + $TaskName + '（用户 ' + $TaskUser + '）')
    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings `
        -User $TaskUser -RunLevel Highest -Force | Out-Null
    if (-not $NoStart) {
        Start-ScheduledTask -TaskName $TaskName
        Write-Log '计划任务已启动'
    }
}

Write-Log '迁移完成'
Write-Log '源目录仍保留作为备份；确认新布局运行正常后再决定是否删除。'
Write-Log ('下一步：用 update_remote_node.ps1 从 GitHub 更新，例如：')
Write-Log ('  .\update_remote_node.ps1 -InstallDir "' + $InstallDir + '" -Mode Git -RepoUrl <仓库> -Ref <分支或tag>')