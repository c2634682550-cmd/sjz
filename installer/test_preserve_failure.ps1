# Injection test: force user-data preservation to fail, then prove the installer
# keeps the data instead of silently deleting it.
#   1) user data still on disk after uninstall
#   2) {localappdata}\SJZ-Control\uninstall_preserve_error.log has FAIL lines
#   3) uninstaller returns non-zero (or at least a readable warning in the log)
# Injection: deny "add subdirectory" on %LOCALAPPDATA%\SJZ-Control so the keep
# dir cannot be created, and deny "write data / add file" on the temp root so
# the rescue rename cannot succeed either.
# ASCII only: PS 5.1 reads UTF-8-without-BOM scripts as ANSI.

$ErrorActionPreference = "Continue"
$proj = Split-Path -Parent $PSScriptRoot
$setups = Get-ChildItem (Join-Path $proj "dist-installer\*.exe") -ErrorAction SilentlyContinue |
    Sort-Object Name
if (-not $setups) { "SETUP_MISSING"; exit 1 }
$Setup = $setups[-1].FullName

$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$root = Join-Path $env:TEMP ("sjz-inj-" + $stamp)
$app = Join-Path $root "app"
$local = Join-Path $env:LOCALAPPDATA "SJZ-Control"
New-Item -ItemType Directory $root -Force | Out-Null
New-Item -ItemType Directory $local -Force | Out-Null
$user = $env:USERNAME

# ---- 1. install to a temp dir (never Program Files) ----
$p = Start-Process $Setup -ArgumentList "/CURRENTUSER", "/VERYSILENT", "/SUPPRESSMSGBOXES",
    "/NORESTART", "/DIR=$app", "/LOG=$(Join-Path $root 'install.log')" -PassThru -Wait
"INJ_INSTALL_RC=" + $p.ExitCode

# ---- 2. user data that must survive ----
New-Item -ItemType Directory (Join-Path $app "data\ledger") -Force | Out-Null
"row" | Out-File (Join-Path $app "data\ledger\records-2026-11-07.json") -Encoding utf8
New-Item -ItemType Directory (Join-Path $app "templates\buy") -Force | Out-Null
"png" | Out-File (Join-Path $app "templates\buy\btn.png") -Encoding ascii
"cfg" | Out-File (Join-Path $app "config.json") -Encoding utf8
"INJ_DATA_BEFORE=" + @(Get-ChildItem (Join-Path $app "data") -Recurse -File).Count

# ---- 3. inject failure on both the keep dir and the rescue path ----
& icacls $local /deny "$($user):(AD)" | Out-Null
& icacls $root /deny "$($user):(WD)" | Out-Null

# ---- 4. silent uninstall (must not hang) ----
$un = Join-Path $app "unins000.exe"
$ulog = Join-Path $env:TEMP ("sjz-inj-uninstall-" + $stamp + ".log")
$p2 = Start-Process $un -ArgumentList "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART",
    "/LOG=$ulog" -PassThru -WindowStyle Hidden
$done = $false
try { Wait-Process -Id $p2.Id -Timeout 90 -ErrorAction Stop; $done = $true } catch { }
if (-not $done) { try { Stop-Process -Id $p2.Id -Force } catch { } }
"INJ_UNINSTALL_RC=" + $(if ($done) { $p2.ExitCode } else { "timeout" })
if (Test-Path $ulog) {
    "INJ_UNINSTALL_LOG_TAIL=" + ((Get-Content $ulog -Tail 8) -join " | ")
    Remove-Item $ulog -Force -ErrorAction SilentlyContinue
}

# ---- 5. assertions ----
"INJ_APP_EXISTS=" + (Test-Path $app)
"INJ_PROGRAM_KEPT=" + (Test-Path (Join-Path $app "SJZ-Control.exe"))
"INJ_UNINSTALLER_KEPT=" + (Test-Path (Join-Path $app "unins000.exe"))
"INJ_DATA_FILES=" + @(Get-ChildItem (Join-Path $app "data") -Recurse -File -ErrorAction SilentlyContinue).Count
"INJ_DATA_KEPT=" + (Test-Path (Join-Path $app "data\ledger\records-2026-11-07.json"))
"INJ_TPL_KEPT=" + (Test-Path (Join-Path $app "templates\buy\btn.png"))
"INJ_CFG_KEPT=" + (Test-Path (Join-Path $app "config.json"))
if (Test-Path (Join-Path $app "preserve_trace.txt")) {
    "INJ_TRACE=" + ((Get-Content (Join-Path $app "preserve_trace.txt") -Raw) -replace "`r?`n", " | ")
}
$logf = Join-Path $local "uninstall_preserve_error.log"
"INJ_ERRLOG=" + (Test-Path $logf)
if (Test-Path $logf) {
    "INJ_ERRLOG_CONTENT=" + ((Get-Content $logf -Raw) -replace "`r?`n", " | ")
}
"INJ_FAIL_MARKER=" + (Test-Path (Join-Path $local "uninstall_preserve_failed"))
"INJ_KEEP_DIRS=" + @(Get-ChildItem $local -Directory -Filter "uninstalled-*" -ErrorAction SilentlyContinue).Count
$rescued = @(Get-ChildItem $root -Directory -Filter "SJZ-Control-rescued-*" -ErrorAction SilentlyContinue)
"INJ_RESCUE_DIRS=" + $rescued.Count
$rescuedLedger = $false
foreach ($r in $rescued) {
    if (Test-Path (Join-Path $r.FullName "ledger\records-2026-11-07.json")) { $rescuedLedger = $true }
}
"INJ_RESCUE_HAS_LEDGER=" + $rescuedLedger
"INJ_SHORTCUT_GONE=" + (-not (Test-Path (Join-Path ([Environment]::GetFolderPath("Programs")) "SJZ-Control\SJZ Control.lnk")))

# verdict: data survived somewhere, error log written, uninstall did not succeed silently
$verdict = (Test-Path $app) -and (Test-Path (Join-Path $app "config.json")) -and
    ((Test-Path (Join-Path $app "data\ledger\records-2026-11-07.json")) -or $rescuedLedger) -and
    (Test-Path $logf) -and ((Get-Content $logf -Raw) -match "FAIL") -and ($p2.ExitCode -ne 0)
"INJ_VERDICT=" + $(if ($verdict) { "PASS" } else { "FAIL" })

# ---- 6. restore ACLs, clean up ----
& icacls $local /remove:d $user | Out-Null
& icacls $root /remove:d $user | Out-Null
try { Remove-Item $root -Recurse -Force -ErrorAction SilentlyContinue } catch { }
"INJ_CLEANED=" + (-not (Test-Path $root))
