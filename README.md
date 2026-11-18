# aiui-executor

AI UI 测试平台执行机：主动连接父平台网关、领取任务、运行 MCP/Chromium、回传日志和结果。

## 推荐布局

```
D:/AIUI/
  deploy/     本仓库 deploy/ 下的脚本（长期保留，不随更新替换）
  app/        本仓库代码（部署/更新时整体切换）
  runtime/    remote-node.json、secrets、remote-nodes、logs
  browsers/   Playwright Chromium 或系统浏览器路径
```

## 首次部署

把本仓库 `deploy/` 下的四个脚本放到执行机 `D:/AIUI/deploy/`，然后：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File D:/AIUI/deploy/install_remote_node.ps1 `
  -PlatformUrl https://<父平台主机>:8010 `
  -NodeId win-exec-02 -MachineId win-server-02 `
  -TokenFile D:/AIUI/incoming/win-exec-02.token `
  -Mode Git -RepoUrl https://github.com/Cchenyw/aiui-executor.git -Ref main
```

仓库有 release 后改用 `-Mode Release -Ref latest`。完整说明见
`docs/remote-node-deployment.md`。

## 更新

生产执行机不允许 Git 时，使用版本化制品包（推荐）：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File D:/AIUI/deploy/update_remote_node.ps1 `
  -InstallDir D:/AIUI -Mode Package `
  -PackagePath D:/AIUI/incoming/aiui-executor-0.2.0.zip
```

制品包与 `SHA256SUMS` 放在同一目录；也可用 `-PackageSha256` 显式指定。
`-PackageUrl` 支持内部 HTTP(S) 制品地址。能访问 GitHub Release 时可继续使用
`-Mode Release -RepoUrl https://github.com/Cchenyw/aiui-executor.git -Ref <tag>`。

构建制品：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/build_release.ps1 `
  -Version 0.2.0 -CommonDir D:/path/to/aiui-common
```

构建和发布流程见 `docs/remote-node-deployment.md`。

## Python

脚本默认使用 `py -3`（要求 64 位 Python >= 3.10），也可用 `-PythonVersion 3.13`
或 `-PythonPath` 指定。

开发协作规范见 [AGENTS.md](AGENTS.md)，执行端实现约束见 [CLAUDE.md](CLAUDE.md)。
