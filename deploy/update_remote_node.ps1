#requires -Version 5.1
<#
执行机更新脚本：从 GitHub 拉取指定版本的执行机代码到 staging，安装依赖并自检，
然后停止计划任务、切换 app 目录、启动任务；失败自动回滚。
运行数据目录 runtime\ 全程不修改。

推荐布局（代码与运行数据分离）：
  D:\AIUI\app      代码，更新时整体替换
  D:\AIUI\runtime  remote-node.json、secrets、remote-nodes、logs

当前用法（从拆分后的执行机仓库 main 拉取）：
  .\update_remote_node.ps1 -Mode Git `
    -RepoUrl https://github.com/Cchenyw/aiui-executor.git -Ref main

发布 release 后（固定 release tag）：
  .\update_remote_node.ps1 -Mode Release -RepoUrl https://github.com/Cchenyw/aiui-executor.git -Ref latest

无 Git / 内网制品包（推荐给不允许 Git 的执行机）：
  .\update_remote_node.ps1 -Mode Package -PackagePath D:\incoming\aiui-executor-0.2.0.zip
  # 默认读取同目录 SHA256SUMS；也可用 -PackageSha256 或显式 -SkipChecksum
#>
param(
    [string]$InstallDir = 'D:\AIUI',
    [string]$AppDir = '',
    [string]$RuntimeDir = '',
    [string]$RepoUrl = 'https://github.com/Cchenyw/aiui-executor.git',
    [string]$Ref = 'latest',
    [ValidateSet('Release', 'Git', 'Package')][string]$Mode = 'Release',
    [string[]]$SparsePaths = @(),
    [string]$PackagePath = '',
    [string]$PackageUrl = '',
    [string]$PackageSha256 = '',
    [switch]$SkipChecksum,
    [string]$PythonPath = '',
    [string]$PythonVersion = '',
    [string]$Wheelhouse = '',
    [string]$BrowsersPath = '',
    [switch]$SkipBrowserInstall,
    [string]$TaskName = 'AIUI Remote Node',
    [int]$KeepBackups = 3,
    [switch]$SkipDeps,
    [switch]$SkipSelfCheck,
    [switch]$SkipGatewayCheck,
    [switch]$NoRestart,
    [switch]$ForceStop,
    [switch]$DryRun,
    [string]$GitHubTokenEnv = 'AIUI_GITHUB_TOKEN'
)

$ErrorActionPreference = 'Stop'
if ($SparsePaths.Count -eq 1 -and $SparsePaths[0] -match ',') {
    $SparsePaths = @($SparsePaths[0] -split ',')
}
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
$env:PYTHONIOENCODING = 'utf-8'
$env:PYTHONUTF8 = '1'

$InstallDir = [System.IO.Path]::GetFullPath($InstallDir)
if (-not $AppDir) { $AppDir = Join-Path $InstallDir 'app' }
if (-not $RuntimeDir) { $RuntimeDir = Join-Path $InstallDir 'runtime' }
$AppDir = [System.IO.Path]::GetFullPath($AppDir)
$RuntimeDir = [System.IO.Path]::GetFullPath($RuntimeDir)
$configPath = Join-Path $RuntimeDir 'remote-node.json'
$logDir = Join-Path $RuntimeDir 'logs'
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$logFile = Join-Path $logDir ('update-' + $stamp + '.log')

function Write-Log([string]$message) {
    $line = '[' + (Get-Date -Format 'HH:mm:ss') + '] ' + $message
    Write-Host $line
    Add-Content -LiteralPath $logFile -Value $line -Encoding utf8
}

function Get-WorkerProcesses {
    @(Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='pythonw.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -and $_.CommandLine -match 'aiui\.agent\.remote' })
}

function Get-NodeTask {
    Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
}

function Stop-Node {
    $task = Get-NodeTask
    if ($task) {
        Write-Log ("停止计划任务: " + $TaskName)
        Stop-ScheduledTask -TaskName $TaskName
        for ($i = 0; $i -lt 30; $i++) {
            Start-Sleep -Seconds 1
            if (-not (Get-WorkerProcesses)) {
                Write-Log '节点进程已退出'
                return
            }
        }
        if ($ForceStop) {
            Get-WorkerProcesses | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
            Start-Sleep -Seconds 2
            if (-not (Get-WorkerProcesses)) { return }
        }
        throw '计划任务已停止，但节点进程仍在运行；请检查后重试，或加 -ForceStop。'
    }
    $procs = @(Get-WorkerProcesses)
    if ($procs.Count -eq 0) {
        Write-Log '未检测到正在运行的节点进程'
        return
    }
    $pids = (($procs | ForEach-Object { $_.ProcessId }) -join ', ')
    if ($ForceStop) {
        Write-Log ("强制结束节点进程: " + $pids)
        $procs | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
        Start-Sleep -Seconds 2
        if (-not (Get-WorkerProcesses)) { return }
        throw '强制结束后仍有节点进程在运行'
    }
    throw ("检测到正在运行的远程节点进程（PID: " + $pids + "）。请先停止该窗口/进程后重试，或使用 -ForceStop。")
}

function Start-Node {
    if ($NoRestart) {
        Write-Log '已指定 -NoRestart，跳过启动'
        return
    }
    if (Get-NodeTask) {
        Start-ScheduledTask -TaskName $TaskName
        Write-Log ("已启动计划任务: " + $TaskName)
        return
    }
    $launcher = Join-Path $RuntimeDir 'start-node.ps1'
    if (Test-Path -LiteralPath $launcher) {
        Write-Log ('未找到计划任务；请手动启动: powershell -NoProfile -ExecutionPolicy Bypass -File "' + $launcher + '"')
        return
    }
    $cmd = 'powershell -NoProfile -ExecutionPolicy Bypass -File "' + (Join-Path $AppDir 'scripts/start_remote_node.ps1') +
        '" -Config "' + $configPath + '" -RuntimeDir "' + $RuntimeDir + '"'
    if ($BrowsersPath) { $cmd += ' -BrowsersPath "' + $BrowsersPath + '"' }
    Write-Log ('未找到计划任务；请手动启动: ' + $cmd)
}

function Resolve-GitHubSlug([string]$url) {
    $clean = $url.Trim().TrimEnd('/')
    if ($clean -match 'github\.com[:/](?<owner>[^/]+)/(?<repo>[^/]+?)(?:\.git)?$') {
        return @($matches['owner'], ($matches['repo'] -replace '\.git$', ''))
    }
    throw ("无法解析 GitHub 仓库地址: " + $url)
}

function Get-GitHubHeaders {
    $headers = @{ 'User-Agent' = 'aiui-executor-updater' }
    $token = [Environment]::GetEnvironmentVariable($GitHubTokenEnv)
    if ($token) { $headers['Authorization'] = 'Bearer ' + $token }
    return $headers
}

function Expand-ArchiveFlat([string]$zipPath, [string]$destination) {
    Expand-Archive -LiteralPath $zipPath -DestinationPath $destination -Force
    $entries = @(Get-ChildItem -LiteralPath $destination -Force)
    if ($entries.Count -eq 1 -and $entries[0].PSIsContainer) {
        $top = $entries[0].FullName
        Get-ChildItem -LiteralPath $top -Force | ForEach-Object {
            Move-Item -LiteralPath $_.FullName -Destination $destination
        }
        Remove-Item -LiteralPath $top -Force
    }
}

function Get-ExpectedPackageHash([string]$zipPath) {
    if ($PackageSha256) { return $PackageSha256.Trim().ToLower() }
    $name = Split-Path -Leaf $zipPath
    $sums = Join-Path (Split-Path -Parent $zipPath) 'SHA256SUMS'
    if (Test-Path -LiteralPath $sums) {
        foreach ($line in Get-Content -LiteralPath $sums) {
            $parts = $line -split '\s+', 2
            if ($parts.Count -eq 2 -and $parts[1].Trim().TrimStart('*') -eq $name) {
                return $parts[0].Trim().ToLower()
            }
        }
    }
    $side = $zipPath + '.sha256'
    if (Test-Path -LiteralPath $side) {
        $first = Get-Content -LiteralPath $side -TotalCount 1
        if ($first) { return (($first -split '\s+')[0]).Trim().ToLower() }
    }
    return $null
}

function Get-StagingCode {
    if (Test-Path -LiteralPath $staging) {
        Remove-Item -LiteralPath $staging -Recurse -Force
    }
    if ($Mode -eq 'Package') {
        $download = $false
        if ($PackagePath) {
            $zipPath = [System.IO.Path]::GetFullPath($PackagePath)
            if (-not (Test-Path -LiteralPath $zipPath)) { throw ('安装包不存在: ' + $zipPath) }
        } elseif ($PackageUrl) {
            $zipPath = Join-Path ([System.IO.Path]::GetTempPath()) ('aiui-package-' + [guid]::NewGuid().ToString('N') + '.zip')
            $download = $true
            Invoke-WebRequest -Uri $PackageUrl -OutFile $zipPath -UseBasicParsing
        } else {
            throw 'Package 模式需要 -PackagePath 或 -PackageUrl'
        }
        try {
            $hash = (Get-FileHash -LiteralPath $zipPath -Algorithm SHA256).Hash.ToLower()
            $expected = Get-ExpectedPackageHash $zipPath
            if ($expected -and $expected -ne $hash) {
                throw ('SHA256 校验失败: 期望 ' + $expected + '，实际 ' + $hash)
            }
            if (-not $expected) {
                if (-not $SkipChecksum) {
                    throw '安装包缺少校验值：请提供 -PackageSha256、同目录 SHA256SUMS，或显式 -SkipChecksum'
                }
                Write-Log '[WARN] 未提供安装包 SHA256，已按 -SkipChecksum 跳过校验'
            }
            Write-Log ('安装包: ' + (Split-Path -Leaf $zipPath) + ' (SHA256 ' + $hash + ')')
            New-Item -ItemType Directory -Force -Path $staging | Out-Null
            Expand-ArchiveFlat $zipPath $staging
            Write-Log ('Package 检出: ' + (Split-Path -Leaf $zipPath))
        } finally {
            if ($download) { Remove-Item -LiteralPath $zipPath -Force -ErrorAction SilentlyContinue }
        }
        return
    }
    if ($Mode -eq 'Git') {
        $git = Get-Command git -ErrorAction SilentlyContinue
        if (-not $git) { throw '未找到 git，请安装 Git 或改用 -Mode Release。' }
        $ref = $Ref
        if (-not $ref -or $ref -eq 'latest') { $ref = 'main' }
        $cloneArgs = @('clone', '--depth', '1')
        if ($SparsePaths.Count -gt 0) { $cloneArgs += @('--filter=blob:none', '--sparse') }
        $cloneArgs += @('--branch', $ref, $RepoUrl, $staging)
        & git @cloneArgs
        if ($LASTEXITCODE -ne 0) { throw ("git clone 失败（退出码 " + $LASTEXITCODE + "）") }
        if ($SparsePaths.Count -gt 0) {
            & git -C $staging sparse-checkout set @SparsePaths
            if ($LASTEXITCODE -ne 0) { throw ("git sparse-checkout 失败（退出码 " + $LASTEXITCODE + "）") }
        }
        $commit = ((& git -C $staging rev-parse HEAD) | Select-Object -First 1)
        if ($commit) { $commit = $commit.Trim() }
        Write-Log ("Git 检出: " + $RepoUrl + " @ " + $ref + " (" + $commit + ")")
        Remove-Item -LiteralPath (Join-Path $staging '.git') -Recurse -Force -ErrorAction SilentlyContinue
        return
    }

    $slug = Resolve-GitHubSlug $RepoUrl
    $owner = $slug[0]
    $repo = $slug[1]
    $headers = Get-GitHubHeaders
    $apiBase = 'https://api.github.com/repos/' + $owner + '/' + $repo
    try {
        if ($Ref -eq 'latest') {
            $release = Invoke-RestMethod -Uri ($apiBase + '/releases/latest') -Headers $headers -TimeoutSec 30
        } else {
            $release = Invoke-RestMethod -Uri ($apiBase + '/releases/tags/' + $Ref) -Headers $headers -TimeoutSec 30
        }
    } catch {
        throw ('获取 GitHub release 失败：' + $_.Exception.Message + '。请检查仓库、-Ref、网络或令牌。')
    }
    $tag = $release.tag_name
    $assets = @($release.assets)
    $zipAsset = $assets | Where-Object { $_.name -match '\.zip$' } | Select-Object -First 1
    if ($zipAsset) {
        $zipUrl = $zipAsset.browser_download_url
        $zipName = $zipAsset.name
    } else {
        $zipUrl = 'https://github.com/' + $owner + '/' + $repo + '/archive/refs/tags/' + $tag + '.zip'
        $zipName = $repo + '-' + $tag + '.zip'
    }
    $zipPath = Join-Path ([System.IO.Path]::GetTempPath()) ('aiui-update-' + [guid]::NewGuid().ToString('N') + '.zip')
    try {
        Invoke-WebRequest -Uri $zipUrl -Headers $headers -OutFile $zipPath -UseBasicParsing
        $hash = (Get-FileHash -LiteralPath $zipPath -Algorithm SHA256).Hash.ToLower()
        Write-Log ('下载: ' + $zipName + ' (SHA256 ' + $hash + ')')
        $sumAsset = $assets | Where-Object { $_.name -eq 'SHA256SUMS' } | Select-Object -First 1
        if ($sumAsset) {
            $sumPath = Join-Path ([System.IO.Path]::GetTempPath()) ('aiui-sums-' + [guid]::NewGuid().ToString('N') + '.txt')
            try {
                Invoke-WebRequest -Uri $sumAsset.browser_download_url -Headers $headers -OutFile $sumPath -UseBasicParsing
                $expected = $null
                foreach ($line in Get-Content -LiteralPath $sumPath) {
                    $parts = $line -split '\s+', 2
                    if ($parts.Count -eq 2 -and $parts[1].Trim().TrimStart('*') -eq $zipName) {
                        $expected = $parts[0].Trim().ToLower()
                        break
                    }
                }
                if ($expected -and $expected -ne $hash) {
                    throw ('SHA256 校验失败: 期望 ' + $expected + '，实际 ' + $hash)
                }
                if (-not $expected) { Write-Log 'SHA256SUMS 中没有当前 zip 的记录，已跳过校验' }
            } finally {
                Remove-Item -LiteralPath $sumPath -Force -ErrorAction SilentlyContinue
            }
        }
        New-Item -ItemType Directory -Force -Path $staging | Out-Null
        Expand-ArchiveFlat $zipPath $staging
    } finally {
        Remove-Item -LiteralPath $zipPath -Force -ErrorAction SilentlyContinue
    }
    Write-Log ('Release 检出: ' + $owner + '/' + $repo + ' @ ' + $tag)
}

function Install-Dependencies {
    if ($SkipDeps) {
        if (-not $SkipSelfCheck) {
            throw '指定了 -SkipDeps 但没有 -SkipSelfCheck；staging 没有 venv，无法自检。'
        }
        Write-Log '已跳过依赖安装'
        return
    }
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
    $venvDir = Join-Path $staging '.venv'
    & $pythonExe @pythonArgs -m venv $venvDir
    if ($LASTEXITCODE -ne 0) { throw '创建虚拟环境失败' }
    $venvPython = Join-Path $venvDir 'Scripts\python.exe'
    & $venvPython -m ensurepip --upgrade
    & $venvPython -m pip install --upgrade pip setuptools wheel
    if ($LASTEXITCODE -ne 0) { throw '升级 pip 失败' }
    $pipArgs = @('-m', 'pip', 'install', '-r', (Join-Path $staging 'requirements.txt'))
    if ($Wheelhouse) { $pipArgs += @('--no-index', '--find-links', $Wheelhouse) }
    & $venvPython @pipArgs
    if ($LASTEXITCODE -ne 0) { throw '安装依赖失败' }
    & $venvPython -m pip check
    if ($LASTEXITCODE -ne 0) { throw 'pip check 失败' }
    if (-not $SkipBrowserInstall -and (Test-Path -LiteralPath $configPath)) {
        $cfg = Get-Content -LiteralPath $configPath -Raw | ConvertFrom-Json
        if ($cfg.chromium) {
            $SkipBrowserInstall = $true
            Write-Log ('配置已指定 chromium，跳过 Playwright Chromium 安装: ' + $cfg.chromium)
        }
    }
    if ($BrowsersPath) {
        New-Item -ItemType Directory -Force -Path $BrowsersPath | Out-Null
        $env:PLAYWRIGHT_BROWSERS_PATH = $BrowsersPath
    }
    if ($SkipBrowserInstall) {
        Write-Log '已跳过 Playwright Chromium 安装'
    } else {
        & $venvPython -m playwright install chromium
        if ($LASTEXITCODE -ne 0) { throw 'Playwright Chromium 安装失败' }
    }
    Write-Log '依赖安装完成'
}

function Invoke-SelfCheck {
    if ($SkipSelfCheck) {
        Write-Log '已跳过自检'
        return
    }
    $script = Join-Path $PSScriptRoot 'remote_selfcheck.ps1'
    if (-not (Test-Path -LiteralPath $script)) {
        $script = Join-Path $staging 'deploy/remote_selfcheck.ps1'
    }
    if (-not (Test-Path -LiteralPath $script)) {
        $script = Join-Path $staging 'scripts/remote_selfcheck.ps1'
    }
    if (-not (Test-Path -LiteralPath $script)) { throw ('缺少自检脚本: ' + $script) }
    $outDir = Join-Path $RuntimeDir ('logs\selfcheck-' + $stamp)
    $checkArgs = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $script,
        '-ProjectRoot', $staging, '-ConfigPath', $configPath, '-OutputDir', $outDir)
    if ($SkipGatewayCheck -or $DryRun) { $checkArgs += '-SkipGatewayCheck' }
    & powershell @checkArgs
    if ($LASTEXITCODE -ne 0) { throw ('staging 自检失败（退出码 ' + $LASTEXITCODE + '），未切换版本') }
    Write-Log 'staging 自检通过'
}

Write-Log '===== AIUI 执行机更新 ====='
Write-Log ('模式: ' + $Mode + ' | 仓库: ' + $RepoUrl + ' | 版本: ' + $Ref)
Write-Log ('App: ' + $AppDir)
Write-Log ('Runtime: ' + $RuntimeDir)

if (-not (Test-Path -LiteralPath $InstallDir)) { throw ('安装目录不存在: ' + $InstallDir) }
if (-not (Test-Path -LiteralPath $AppDir)) { throw ('app 目录不存在: ' + $AppDir + '；请先执行安装/迁移脚本。') }
if (-not (Test-Path -LiteralPath $configPath)) { throw ('缺少运行配置: ' + $configPath) }
$sep = [System.IO.Path]::DirectorySeparatorChar
if ($RuntimeDir -eq $AppDir -or $RuntimeDir.StartsWith($AppDir + $sep, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw 'runtime 目录位于 app 目录内，无法安全整体替换；请先迁移到 app/runtime 分离布局。'
}

$staging = Join-Path $InstallDir ('app.staging-' + $stamp)
$backup = Join-Path $InstallDir ('app.prev-' + $stamp)
$failed = Join-Path $InstallDir ('app.failed-' + $stamp)
$swapped = $false
$stopped = $false

try {
    Get-ChildItem -LiteralPath $InstallDir -Directory -Filter 'app.staging-*' -ErrorAction SilentlyContinue |
        ForEach-Object { Remove-Item -LiteralPath $_.FullName -Recurse -Force -ErrorAction SilentlyContinue }
    Get-StagingCode
    if (-not (Test-Path -LiteralPath (Join-Path $staging 'requirements.txt')) -or
        -not (Test-Path -LiteralPath (Join-Path $staging 'aiui'))) {
        throw '拉取到的代码缺少 requirements.txt 或 aiui 目录，请检查 -SparsePaths / release 内容。'
    }
    Install-Dependencies

    if ($DryRun) {
        Invoke-SelfCheck
        Write-Log ('DryRun 完成，未停止/切换服务。staging: ' + $staging)
        exit 0
    }

    Stop-Node
    $stopped = $true
    Invoke-SelfCheck

    Write-Log ('切换 app 目录: ' + $AppDir + ' -> ' + $backup)
    Rename-Item -LiteralPath $AppDir -NewName (Split-Path -Leaf $backup)
    Rename-Item -LiteralPath $staging -NewName (Split-Path -Leaf $AppDir)
    $swapped = $true
    Start-Node
    Write-Log '更新完成'
} catch {
    $message = $_.Exception.Message
    Write-Log ('更新失败: ' + $message)
    if ($swapped) {
        Write-Log '回滚到上一个版本'
        if (Test-Path -LiteralPath $AppDir) {
            Rename-Item -LiteralPath $AppDir -NewName (Split-Path -Leaf $failed) -ErrorAction SilentlyContinue
        }
        if (Test-Path -LiteralPath $backup) {
            Rename-Item -LiteralPath $backup -NewName (Split-Path -Leaf $AppDir) -ErrorAction SilentlyContinue
        }
        Start-Node
    } elseif ($stopped) {
        Write-Log '本次未切换 app，重新启动原版本'
        Start-Node
    }
    throw
} finally {
    Get-ChildItem -LiteralPath $InstallDir -Directory -Filter 'app.prev-*' -ErrorAction SilentlyContinue |
        Sort-Object Name -Descending | Select-Object -Skip $KeepBackups |
        ForEach-Object { Remove-Item -LiteralPath $_.FullName -Recurse -Force -ErrorAction SilentlyContinue }
    Write-Log ('日志: ' + $logFile)
}