<#
tools/kill_stray.ps1  ——  dafors 残留进程清扫

背景：2026-11-07 后台残留 23 个 `python -c "while True: pass"` 忙等进程，
11 个满核把 CPU 推到 90%。本脚本负责把这类残留找出来并清掉。

扫描三类（只认白名单，绝不按进程名批量杀 python）：
  1. 命令行含 `while True: pass` / `while 1: pass` 的裸忙等
  2. `load_gen.py` 进程：父进程超时未退 → 杀；worker 父进程已死（孤儿）→ 杀
  3. 采集/压测进程（soak / longrun_collect / bench_* / collect_* /
     realmachine_* / telemetry_agent）已跑超过自己声明的
     `--duration` / `--seconds` / `--minutes` + 宽限秒数 → 杀

默认 dry-run 只打印；加 -Force 才真的 Stop-Process。

用法：
    powershell -NoProfile -ExecutionPolicy Bypass -File tools\kill_stray.ps1
    powershell -NoProfile -ExecutionPolicy Bypass -File tools\kill_stray.ps1 -Force
    powershell -NoProfile -ExecutionPolicy Bypass -File tools\kill_stray.ps1 -Force -GraceSeconds 10
#>

[CmdletBinding()]
param(
    [switch]$Force,
    [int]$GraceSeconds = 30,
    [int]$SampleMs = 500
)

$ErrorActionPreference = 'Continue'

$OverrunScriptPattern = 'tools[\\/](soak|bench_[a-z0-9_]+|collect_[a-z0-9_]+|realmachine_[a-z0-9_]+|telemetry_agent)\.py|-m\s+tools\.longrun_collect'

function Get-DeclaredSeconds {
    param([string]$Cmd)
    if ($Cmd -match '--duration\s+([0-9]+(?:\.[0-9]+)?)') { return [double]$Matches[1] }
    if ($Cmd -match '--seconds\s+([0-9]+(?:\.[0-9]+)?)')  { return [double]$Matches[1] }
    if ($Cmd -match '--minutes\s+([0-9]+(?:\.[0-9]+)?)')  { return ([double]$Matches[1]) * 60 }
    return $null
}

function Get-PythonCpuPct {
    param([int]$Ms = 500)
    $snap = @{}
    foreach ($p in @(Get-Process -Name python -ErrorAction SilentlyContinue)) {
        $snap[$p.Id] = $p.TotalProcessorTime.TotalMilliseconds
    }
    Start-Sleep -Milliseconds $Ms
    $cores = [int](Get-CimInstance Win32_ComputerSystem -ErrorAction SilentlyContinue).NumberOfLogicalProcessors
    if ($cores -lt 1) { $cores = 1 }
    $sum = 0.0
    foreach ($p in @(Get-Process -Name python -ErrorAction SilentlyContinue)) {
        if ($snap.ContainsKey($p.Id)) {
            $d = $p.TotalProcessorTime.TotalMilliseconds - $snap[$p.Id]
            if ($d -gt 0) { $sum += $d }
        }
    }
    return [Math]::Round(($sum / ($Ms * $cores)) * 100, 1)
}

function Get-SystemCpuPct {
    try {
        $v = (Get-CimInstance Win32_PerfFormattedData_PerfOS_Processor -Filter "Name='_Total'" -ErrorAction Stop).PercentProcessorTime
        if ($null -ne $v) { return [double]$v }
    } catch { }
    return -1
}

Write-Output "===== kill_stray $(if ($Force) { '[FORCE]' } else { '[DRY-RUN]' }) ====="
$before = @(Get-Process -Name python -ErrorAction SilentlyContinue)
$beforeCount = $before.Count
$beforeCpu = Get-PythonCpuPct -Ms $SampleMs
$sysBefore = Get-SystemCpuPct
Write-Output ("清理前：python.exe = {0} 个；python 合计 CPU = {1}%；整机 CPU = {2}%" -f $beforeCount, $beforeCpu, $sysBefore)
Write-Output ""

$now = Get-Date
$rows = @()

foreach ($p in @(Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue)) {
    $cmd = [string]$p.CommandLine
    if ([string]::IsNullOrWhiteSpace($cmd)) { continue }
    $elapsed = if ($p.CreationDate) { [Math]::Round(($now - $p.CreationDate).TotalSeconds, 1) } else { -1 }
    $verdict = $null
    $killable = $false

    if ($cmd -match 'while\s*(True|1)\s*:\s*pass|while\s+True\s*:\s*pass') {
        $verdict = '裸忙等 while True: pass'
        $killable = $true
    }
    elseif ($cmd -match 'load_gen\.py') {
        if ($cmd -match '--_worker') {
            $parentPid = [int]$p.ParentProcessId
            $parentAlive = [bool](Get-Process -Id $parentPid -ErrorAction SilentlyContinue)
            if ($parentAlive) {
                $verdict = "load_gen worker（父 $parentPid 存活）"
            } else {
                $verdict = "load_gen worker 孤儿（父 $parentPid 已死）"
                $killable = $true
            }
        } else {
            $decl = Get-DeclaredSeconds $cmd
            if ($null -ne $decl -and $elapsed -gt ($decl + $GraceSeconds)) {
                $verdict = "load_gen 超时（声明 {0}s + 宽限 {1}s）" -f $decl, $GraceSeconds
                $killable = $true
            } else {
                $verdict = 'load_gen 受控运行中'
            }
        }
    }
    elseif ($cmd -match 'prec_obs_|tools[\\/]fire_worker\.py') {
        # 高优先级观测器 / 打到目标时刻的 worker：父进程活着就保留，
        # 父进程没了就是孤儿（观测器还是 HIGH_PRIORITY，必须清掉）
        $parentPid = [int]$p.ParentProcessId
        if ([bool](Get-Process -Id $parentPid -ErrorAction SilentlyContinue)) {
            $verdict = "孤儿类进程（父 $parentPid 存活）"
        } else {
            $verdict = "孤儿类进程（父 $parentPid 已死）"
            $killable = $true
        }
    }
    elseif ($cmd -match $OverrunScriptPattern) {
        $decl = Get-DeclaredSeconds $cmd
        if ($null -ne $decl -and $elapsed -gt ($decl + $GraceSeconds)) {
            $verdict = "超时（声明 {0}s + 宽限 {1}s）" -f $decl, $GraceSeconds
            $killable = $true
        } else {
            $verdict = '采集/压测运行中（未超时）'
        }
    }
    else {
        continue          # 不在白名单，一律不碰
    }

    $rows += [pscustomobject]@{
        PID      = [int]$p.ProcessId
        Elapsed  = $elapsed
        Killable = $killable
        Verdict  = $verdict
        Cmd      = if ($cmd.Length -gt 120) { $cmd.Substring(0, 120) + '...' } else { $cmd }
    }
}

if ($rows.Count -eq 0) {
    Write-Output "未发现匹配的残留进程。"
} else {
    Write-Output "命中白名单进程（每条给出 PID / 已运行秒数 / 处置 / 原因 / 命令行前 120 字符）："
    Write-Output ("-" * 100)
    foreach ($r in $rows) {
        $act = if ($r.Killable) { '可清理' } else { '保留' }
        Write-Output ("PID {0}  已运行 {1}s  [{2}]  {3}" -f $r.PID, $r.Elapsed, $act, $r.Verdict)
        Write-Output ("    cmd: {0}" -f $r.Cmd)
    }
}
Write-Output ""

$targets = @($rows | Where-Object { $_.Killable })
if ($Force) {
    if ($targets.Count -eq 0) {
        Write-Output "没有需要清理的进程。"
    } else {
        foreach ($t in $targets) {
            try {
                Stop-Process -Id $t.PID -Force -ErrorAction Stop
                Write-Output ("已结束 PID {0}（{1}）" -f $t.PID, $t.Verdict)
            } catch {
                Write-Output ("结束 PID {0} 失败：{1}" -f $t.PID, $_.Exception.Message)
            }
        }
        Start-Sleep -Seconds 1
    }
} else {
    Write-Output ("DRY-RUN：命中 {0} 个可清理进程，未杀任何进程。加 -Force 才执行。" -f $targets.Count)
}
Write-Output ""

$after = @(Get-Process -Name python -ErrorAction SilentlyContinue)
$afterCount = $after.Count
$afterCpu = Get-PythonCpuPct -Ms $SampleMs
$sysAfter = Get-SystemCpuPct
Write-Output ("清理后：python.exe = {0} 个；python 合计 CPU = {1}%；整机 CPU = {2}%" -f $afterCount, $afterCpu, $sysAfter)
Write-Output ("对比：数量 {0} -> {1}（{2}）；CPU {3}% -> {4}%；整机 {5}% -> {6}%" -f `
    $beforeCount, $afterCount, ($afterCount - $beforeCount), $beforeCpu, $afterCpu, $sysBefore, $sysAfter)
