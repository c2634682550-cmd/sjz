# 打包/安装前置检查：有没有残留进程占着 dist\ 或安装目录。
# 本机沙箱偶尔会把子进程挂成「杀不掉的幽灵」，锁住 cv2.pyd / onnxruntime / python311.dll，
# 让人误以为是卸载脚本的锅。先把它们点名报出来，并给出结束命令。
param(
  [string[]]$Roots = @(),
  [switch]$Quiet
)
$ErrorActionPreference = "SilentlyContinue"

$here = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$targets = @()
if ($Roots.Count -gt 0) { $targets += $Roots }
$targets += @(
  (Join-Path $here "dist"),
  (Join-Path $here "dist-installer"),
  (Join-Path $here "dist-onefile"),
  (Join-Path $env:LOCALAPPDATA "SJZ-Control")
)

$roots = @()
foreach ($t in $targets) {
  if ($t -and (Test-Path $t)) { $roots += ([System.IO.Path]::GetFullPath($t)).TrimEnd('\') + '\' }
}

$hits = @()
foreach ($p in (Get-CimInstance Win32_Process -ErrorAction SilentlyContinue)) {
  $path = $p.ExecutablePath
  if (-not $path) {
    # 幽灵进程常常拿不到 ExecutablePath：退一步看命令行里有没有我们的目录
    $path = $p.CommandLine
  }
  if (-not $path) { continue }
  foreach ($r in $roots) {
    if ($path.StartsWith($r, [System.StringComparison]::OrdinalIgnoreCase) -or
        $path.IndexOf($r, [System.StringComparison]::OrdinalIgnoreCase) -ge 0) {
      $hits += [pscustomobject]@{ Pid = $p.ProcessId; Name = $p.Name; Path = $path }
      break
    }
  }
}

if ($hits.Count -eq 0) {
  if (-not $Quiet) { Write-Host "  [lock-check] 无残留进程占用 dist/安装目录" }
  exit 0
}

Write-Host ""
Write-Host "  !! 有 $($hits.Count) 个进程正在占用 dist/安装目录："
foreach ($h in $hits) {
  Write-Host ("     pid={0,-6} {1,-18} {2}" -f $h.Pid, $h.Name, $h.Path)
}
Write-Host "  !! 这些进程会锁住 pyd/dll，导致打包覆盖失败或卸载删不干净。"
Write-Host "  !! 结束命令（逐个）："
foreach ($h in $hits) { Write-Host ("     taskkill /F /PID {0}" -f $h.Pid) }
Write-Host "  !! 若 taskkill 报「没有运行实例」而进程仍在，说明是沙箱挂起的幽灵，重启后再跑。"
exit 2
