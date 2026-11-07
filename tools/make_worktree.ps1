# 为并行会话创建一份不可变工作树快照。
#
# 用法（在仓库任意位置）：
#   powershell -ExecutionPolicy Bypass -File tools\make_worktree.ps1 -Name wt-input
#   powershell -ExecutionPolicy Bypass -File tools\make_worktree.ps1 -Name wt-nav -Mode git
#
# 产物：<仓库父目录>\dafors-wt\<Name>\
#   SNAPSHOT.txt         来源 digest / 创建时刻 / 快照 digest
#   SNAPSHOT.files.json  逐文件基线哈希（merge_gate 的 3-way 基线）
#   _snapshot_base\      可文本合并文件的基线副本（冲突判定用）
#
# copy 模式复制当前工作树（含未提交改动），排除了 .git/.venv/build/dist/perf 大目录等；
# git 模式用 git worktree 建一个干净 HEAD 分支，两种模式都会写同一套快照元数据。

param(
    [string]$Name = '',
    [string]$Root = '',
    [string]$Mode = 'copy',
    [switch]$Force
)

$ErrorActionPreference = 'Stop'

if (-not $Name) { throw "缺少 -Name（快照名）" }
if ($Mode -notin @('copy', 'git')) { throw "不支持的 -Mode：$Mode（copy|git）" }
if ($Name -match '[\\/:*?"<>|]' -or $Name -in @('.', '..')) {
    throw "非法快照名：$Name"
}

# -File 调用时 param 默认值里拿不到 $PSScriptRoot，这里补算一次。
if (-not $Root) {
    $scriptDir = $PSScriptRoot
    if (-not $scriptDir) { $scriptDir = [System.IO.Path]::GetDirectoryName($MyInvocation.MyCommand.Path) }
    $Root = [System.IO.Path]::GetDirectoryName($scriptDir)
}
$Root = (Resolve-Path -LiteralPath $Root).Path
$Parent = [System.IO.Path]::GetDirectoryName($Root)
$DestRoot = Join-Path $Parent 'dafors-wt'
$Dest = Join-Path $DestRoot $Name

if (-not (Test-Path -LiteralPath $Root)) { throw "源树不存在：$Root" }
if (-not (Test-Path -LiteralPath (Join-Path $Root 'core'))) { throw "源树里没有 core/：$Root" }

if (Test-Path -LiteralPath $Dest) {
    if (-not $Force) {
        throw "目标已存在：$Dest（要覆盖请加 -Force；这会删除该快照里未合并的改动）"
    }
    $resolved = (Resolve-Path -LiteralPath $Dest).Path
    if (-not $resolved.StartsWith($DestRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "拒绝删除不在 dafors-wt 下的目录：$resolved"
    }
    Remove-Item -LiteralPath $resolved -Recurse -Force
}
New-Item -ItemType Directory -Force -Path $DestRoot | Out-Null

# 选解释器：优先源树的 venv（快照里不复制 .venv，但解释器可以指向源树）
$Py = Join-Path $Root '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $Py)) { $Py = 'python' }
$DigestTool = Join-Path $Root 'tools\tree_digest.py'
if (-not (Test-Path -LiteralPath $DigestTool)) { throw "缺少 tools\tree_digest.py" }

function Get-CoreDigest([string]$TreeRoot) {
    $raw = & $Py $DigestTool --root $TreeRoot --json
    if ($LASTEXITCODE -ne 0) { throw "tree_digest 失败：$TreeRoot" }
    return ($raw | ConvertFrom-Json).digest
}

# 复制/导出前先记源树状态：之后若与快照不一致，说明复制期间正被别的会话写。
$SrcDigest = Get-CoreDigest $Root
$SrcHead = (& git -C $Root rev-parse --short HEAD 2>$null)
$SrcDirty = (& git -C $Root status --porcelain -- core 2>$null | Measure-Object -Line).Lines
$CreatedAt = (Get-Date).ToString('yyyy-MM-ddTHH:mm:sszzz')

Write-Host "[make_worktree] mode=$Mode name=$Name"
Write-Host "[make_worktree] source=$Root  core_digest=$SrcDigest  git=$SrcHead  dirty_core=$SrcDirty"

if ($Mode -eq 'copy') {
    # 与 tools/tree_digest.py 的排除口径保持一致；大目录/运行产物不进快照。
    $xdNames = @(
        '.git', '.venv', '.venv-vl', '__pycache__', '.pytest_cache',
        'build', 'dist', 'dist-installer', 'dist-onefile', 'node_modules',
        'data', '_bmad', '_bmad-output', '_reverse', '_rm_backup', '_rm_shots',
        '_r08_shots', '_ui_shots', '_calib_thumbs', '_verify_bb', '_fe_old_backup',
        '_timing_lab', '_soak_data'
    )
    $xdPaths = @(
        (Join-Path $Root 'perf\snapshots'), (Join-Path $Root 'perf\ocr'),
        (Join-Path $Root 'perf\base'), (Join-Path $Root 'perf\_ref'),
        (Join-Path $Root 'perf\merged'), (Join-Path $Root 'perf\merged_pre'),
        (Join-Path $Root 'perf\out'), (Join-Path $Root 'perf\bench\out'),
        (Join-Path $Root 'evo\runs')
    )
    $xf = @('*.pyc', 'desktop.log', 'ocr_service_*.log')
    Write-Host "[make_worktree] robocopy ...（约 100MB 源码，排除 perf/snapshots 等）"
    & robocopy $Root $Dest /E /R:1 /W:1 /NFL /NDL /NJH /NJS /NP /XD $xdNames $xdPaths /XF $xf | Out-Null
    $rc = $LASTEXITCODE
    # robocopy: 0-7 = 成功（有差异），>=8 = 失败
    if ($rc -ge 8) { throw "robocopy 失败，exit=$rc" }
    Write-Host "[make_worktree] robocopy 完成 exit=$rc"
}
else {
    $branch = "wt/$Name"
    & git -C $Root worktree add -b $branch $Dest HEAD
    if ($LASTEXITCODE -ne 0) {
        throw "git worktree 创建失败（分支 $branch 可能已存在）。可改用 -Mode copy"
    }
    Write-Host "[make_worktree] git worktree 完成 branch=$branch"
}

# 快照元数据从复制后的目录导出：基线 = 快照初始内容，保证 3-way 合并自洽。
$Manifest = Join-Path $Dest 'SNAPSHOT.files.json'
$BaseDir = Join-Path $Dest '_snapshot_base'
& $Py $DigestTool --root $Dest --quiet --emit-manifest $Manifest --emit-base $BaseDir
if ($LASTEXITCODE -ne 0) { throw "导出快照清单失败" }

$DstDigest = Get-CoreDigest $Dest
$Match = ($DstDigest -eq $SrcDigest)

$lines = @(
    "name: $Name",
    "created_at: $CreatedAt",
    "mode: $Mode",
    "source_root: $Root",
    "source_git_head: $SrcHead",
    "source_core_dirty: $SrcDirty",
    "source_core_digest: $SrcDigest",
    "snapshot_core_digest: $DstDigest",
    "digest_match: $($Match.ToString().ToLower())",
    "python: $Py",
    "manifest: SNAPSHOT.files.json",
    "base_blobs: _snapshot_base\",
    "",
    "用法：",
    "  1) 只在本目录改文件；改前打点： $Py tools\tree_digest.py --root . --record",
    "  2) 周期性查看指纹：       $Py tools\tree_digest.py --root . --watch --interval 10",
    "  3) 合回主树（先 dry-run）: $Py <主树>\tools\merge_gate.py --from `"$Dest`"",
    "  4) 确认后加 --apply 实写；冲突文件会被拒绝并单独列出",
    ""
)
[System.IO.File]::WriteAllText((Join-Path $Dest 'SNAPSHOT.txt'),
    ($lines -join "`r`n"), (New-Object System.Text.UTF8Encoding($false)))

Write-Host "[make_worktree] snapshot=$Dest"
Write-Host "[make_worktree] snapshot_digest=$DstDigest  match=$Match"
if (-not $Match) {
    Write-Host "[make_worktree] 警告：源树在复制期间被改动（$SrcDigest -> $DstDigest），" -ForegroundColor Yellow
    Write-Host "[make_worktree] 该快照自身是自洽的，但它不等于任何单一时刻的源树；如需严格锚点请重跑。" -ForegroundColor Yellow
}
Write-Host "[make_worktree] 下一步：cd `"$Dest`"，用源树解释器：$Py"
