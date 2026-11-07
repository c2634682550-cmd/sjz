# Build with the .NET Framework built-in csc (no Visual Studio needed).
# Outputs:
#   SjzLauncher.exe      winexe + asInvoker manifest (release, 日常不提权)
#   SjzLauncherTest.exe  console + asInvoker manifest (CLI only, for pytest)
$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$csc = "$env:WINDIR\Microsoft.NET\Framework64\v4.0.30319\csc.exe"
if (-not (Test-Path $csc)) {
    $csc = "$env:WINDIR\Microsoft.NET\Framework\v4.0.30319\csc.exe"
}
if (-not (Test-Path $csc)) { throw "csc.exe not found (.NET Framework 4.x missing?)" }

$src = Get-ChildItem -Path $here -Filter *.cs | ForEach-Object { $_.FullName }
$refs = @(
    "/r:System.dll",
    "/r:System.Core.dll",
    "/r:System.Windows.Forms.dll",
    "/r:System.Drawing.dll",
    "/r:System.IO.Compression.dll",
    "/r:System.IO.Compression.FileSystem.dll"
)
$common = @("/nologo", "/optimize+", "/platform:anycpu", "/utf8output", "/warn:4") + $refs

Write-Host "==> SjzLauncher.exe (winexe + asInvoker manifest, 日常不提权)"
& $csc @common /target:winexe /out:"$here\SjzLauncher.exe" /win32manifest:"$here\app.manifest" @src
if ($LASTEXITCODE -ne 0) { throw "SjzLauncher.exe build failed" }

Write-Host "==> SjzLauncherTest.exe (console + asInvoker manifest)"
& $csc @common /target:exe /define:SJZ_TEST_BUILD /out:"$here\SjzLauncherTest.exe" /win32manifest:"$here\app.test.manifest" @src
if ($LASTEXITCODE -ne 0) { throw "SjzLauncherTest.exe build failed" }

Get-ChildItem -Path $here -Filter "SjzLauncher*.exe" |
    ForEach-Object { "{0}  {1:N0} bytes" -f $_.Name, $_.Length }
