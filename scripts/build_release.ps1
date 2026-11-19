#requires -Version 5.1
<#
构建 aiui-executor 发布制品：

  aiui-executor-<version>.zip
  SHA256SUMS
  BUILD_INFO.json

制品包含 aiui/agent、aiui/common（vendored）、aiui/toolservers、aiui/testapp、
scripts、configs、deploy、requirements.txt、VERSION 和 README.md；不包含 .venv、
runtime、测试缓存或任何 token/LLM 配置。

用法（在构建机执行，构建机可以使用 Git；执行机不需要 Git）：
  powershell -NoProfile -ExecutionPolicy Bypass -File scripts\build_release.ps1 `
    -Version 0.2.0 -CommonDir D:\PycharmProject\aiui_split\common

发布到内部只读共享或内部制品库后，执行机使用：
  update_remote_node.ps1 -Mode Package -PackagePath <zip路径>
#>
param(
    [string]$SourceDir = (Split-Path -Parent $PSScriptRoot),
    [string]$OutputDir = '',
    [string]$Version = '',
    [string]$CommonDir = '',
    [switch]$KeepStaging
)

$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
$utf8NoBom = New-Object System.Text.UTF8Encoding($false)
$SourceDir = [System.IO.Path]::GetFullPath($SourceDir)
if (-not $OutputDir) { $OutputDir = Join-Path $SourceDir 'dist' }
$OutputDir = [System.IO.Path]::GetFullPath($OutputDir)
New-Item -ItemType Directory -Force -Path $OutputDir | Out-Null

if (-not $Version) {
    $Version = (Get-Content -LiteralPath (Join-Path $SourceDir 'VERSION') -Raw).Trim()
}
$Version = $Version.Trim()
if ($Version.StartsWith('v')) { $Version = $Version.Substring(1) }
if ($Version -notmatch '^[0-9A-Za-z._-]+$') { throw ('版本号格式无效: ' + $Version) }

if (-not $CommonDir) {
    $candidate = Join-Path (Split-Path -Parent $SourceDir) 'common'
    if (Test-Path -LiteralPath $candidate) { $CommonDir = $candidate }
}

$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$staging = Join-Path $OutputDir ('build-' + $Version + '-' + $stamp)
$zipName = 'aiui-executor-' + $Version + '.zip'
$zipPath = Join-Path $OutputDir $zipName
$sumsPath = Join-Path $OutputDir 'SHA256SUMS'
$buildInfoPath = Join-Path $OutputDir 'BUILD_INFO.json'

Write-Host ('构建版本: ' + $Version)
Write-Host ('源码目录: ' + $SourceDir)
Write-Host ('输出目录: ' + $OutputDir)
if ($CommonDir) { Write-Host ('common 目录: ' + $CommonDir) }

New-Item -ItemType Directory -Force -Path $staging | Out-Null
$items = @('aiui', 'scripts', 'configs', 'deploy', 'requirements.txt', 'VERSION', 'README.md')
foreach ($item in $items) {
    $source = Join-Path $SourceDir $item
    if (Test-Path -LiteralPath $source) {
        Copy-Item -LiteralPath $source -Destination $staging -Recurse -Force
    }
}
Get-ChildItem -LiteralPath $staging -Recurse -Directory -ErrorAction SilentlyContinue |
    Where-Object { $_.Name -eq '__pycache__' } |
    Remove-Item -Recurse -Force -ErrorAction SilentlyContinue
Get-ChildItem -LiteralPath $staging -Recurse -File -ErrorAction SilentlyContinue |
    Where-Object { @('.pyc', '.pyo') -contains $_.Extension } |
    Remove-Item -Force -ErrorAction SilentlyContinue
$selfcheckOut = Join-Path $staging 'scripts\selfcheck-output'
if (Test-Path -LiteralPath $selfcheckOut) { Remove-Item -LiteralPath $selfcheckOut -Recurse -Force }

$required = @('aiui\agent\remote.py', 'aiui\common\node_protocol.py',
              'aiui\toolservers\builtin_browser.py', 'requirements.txt', 'deploy\update_remote_node.ps1')
foreach ($rel in $required) {
    if (-not (Test-Path -LiteralPath (Join-Path $staging $rel))) {
        throw ('制品缺少必要文件: ' + $rel)
    }
}

if ($CommonDir -and (Test-Path -LiteralPath $CommonDir)) {
    $commonPy = @(Get-ChildItem -LiteralPath $CommonDir -File -Filter '*.py' -ErrorAction SilentlyContinue)
    foreach ($file in $commonPy) {
        $target = Join-Path $staging ('aiui\common\' + $file.Name)
        if (Test-Path -LiteralPath $target) {
            $a = (Get-FileHash -LiteralPath $file.FullName -Algorithm SHA256).Hash.ToLower()
            $b = (Get-FileHash -LiteralPath $target -Algorithm SHA256).Hash.ToLower()
            if ($a -ne $b) { throw ('common 文件不一致: ' + $file.Name + '（执行机 vendored 副本未同步）') }
        }
    }
    Write-Host ('common 一致性校验通过: ' + $commonPy.Count + ' 个 .py 文件')
} else {
    Write-Host '[WARN] 未提供 common 目录，跳过 common 一致性校验'
}

$versionsFile = Join-Path $staging 'VERSION'
[IO.File]::WriteAllText($versionsFile, $Version + "`n", $utf8NoBom)

function Get-GitCommit([string]$directory) {
    try {
        $git = Get-Command git -ErrorAction SilentlyContinue
        if (-not $git) { return '' }
        $value = (& git -C $directory rev-parse HEAD 2>$null | Select-Object -First 1)
        if ($value) { return $value.Trim() }
    } catch { }
    return ''
}

$commonHash = ''
$commonFile = Join-Path $staging 'aiui\common\node_protocol.py'
if (Test-Path -LiteralPath $commonFile) {
    $commonHash = (Get-FileHash -LiteralPath $commonFile -Algorithm SHA256).Hash.ToLower()
}
$requirementsHash = (Get-FileHash -LiteralPath (Join-Path $staging 'requirements.txt') -Algorithm SHA256).Hash.ToLower()
$executorCommit = Get-GitCommit $SourceDir
$commonCommit = ''
if ($CommonDir) { $commonCommit = Get-GitCommit $CommonDir }
$buildInfo = [ordered]@{
    artifact = $zipName
    version = $Version
    built_at = (Get-Date).ToString('o')
    executor_commit = $executorCommit
    common_commit = $commonCommit
    common_node_protocol_sha256 = $commonHash
    requirements_sha256 = $requirementsHash
}
$buildInfoText = ($buildInfo | ConvertTo-Json -Depth 5)
[IO.File]::WriteAllText((Join-Path $staging 'BUILD_INFO.json'), $buildInfoText + "`n", $utf8NoBom)
[IO.File]::WriteAllText($buildInfoPath, $buildInfoText + "`n", $utf8NoBom)

if (Test-Path -LiteralPath $zipPath) { Remove-Item -LiteralPath $zipPath -Force }
Compress-Archive -Path (Join-Path $staging '*') -DestinationPath $zipPath -CompressionLevel Optimal
$hash = (Get-FileHash -LiteralPath $zipPath -Algorithm SHA256).Hash.ToLower()
[IO.File]::WriteAllText($sumsPath, $hash + '  ' + $zipName + "`n", $utf8NoBom)
[IO.File]::WriteAllText($zipPath + '.sha256', $hash + "`n", $utf8NoBom)

if (-not $KeepStaging) { Remove-Item -LiteralPath $staging -Recurse -Force -ErrorAction SilentlyContinue }

Write-Host ''
Write-Host ('制品: ' + $zipPath)
Write-Host ('SHA256: ' + $hash)
Write-Host ('校验文件: ' + $sumsPath)
Write-Host ('构建信息: ' + $buildInfoPath)
Write-Host '完成。把 zip 和 SHA256SUMS 一起复制到执行机，然后使用 -Mode Package 更新。'
exit 0
