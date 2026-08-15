# Collect the robot/capture evidence needed to diagnose a failed Windows capture.
# This script does not connect to or command the robot. It only reads local app files.

$ErrorActionPreference = "Stop"

function Get-LatestWriteTime([string]$RuntimeRoot) {
    $logs = Join-Path $RuntimeRoot "logs"
    $files = @(
        (Join-Path $logs "diagnostics.log"),
        (Join-Path $logs "backend.log")
    ) | Where-Object { Test-Path -LiteralPath $_ }

    if ($files.Count -eq 0) {
        return [datetime]::MinValue
    }
    return ($files | ForEach-Object { (Get-Item -LiteralPath $_).LastWriteTime } |
        Sort-Object -Descending | Select-Object -First 1)
}

function Get-SafeWebSocketUrl([string]$Value) {
    if ([string]::IsNullOrWhiteSpace($Value)) {
        return "(not found)"
    }
    $safe = $Value -replace '(?<=://)[^/@\s]+@', '<redacted>@'
    return $safe -replace '\?.*$', '?<redacted>'
}

$searchRoots = @($env:APPDATA, $env:LOCALAPPDATA) |
    Where-Object { -not [string]::IsNullOrWhiteSpace($_) -and (Test-Path -LiteralPath $_) } |
    Select-Object -Unique

$runtimeRoots = New-Object System.Collections.Generic.List[string]
foreach ($root in $searchRoots) {
    $knownNames = @(
        "automated-video-editing-frontend",
        "Automated Video Editing",
        "auto-video-editing",
        "com.weiyuech.automatedediting",
        ([string][char]0x81EA + [char]0x52A8 + [char]0x89C6 + [char]0x9891 + [char]0x526A + [char]0x8F91)
    )
    foreach ($name in $knownNames) {
        $candidate = Join-Path (Join-Path $root $name) "runtime"
        if (Test-Path -LiteralPath (Join-Path $candidate "logs")) {
            [void]$runtimeRoots.Add($candidate)
        }
    }

    # Electron userData is normally one directory directly below APPDATA. Checking every
    # direct child avoids depending on the product-name spelling without scanning all AppData.
    Get-ChildItem -LiteralPath $root -Directory -ErrorAction SilentlyContinue | ForEach-Object {
        $candidate = Join-Path $_.FullName "runtime"
        if (Test-Path -LiteralPath (Join-Path $candidate "logs")) {
            [void]$runtimeRoots.Add($candidate)
        }
    }
}

$runtimeRoot = $runtimeRoots |
    Select-Object -Unique |
    Sort-Object -Property @{ Expression = { Get-LatestWriteTime $_ }; Descending = $true } |
    Select-Object -First 1

if ([string]::IsNullOrWhiteSpace($runtimeRoot)) {
    Write-Host "AVE runtime logs were not found under APPDATA or LOCALAPPDATA." -ForegroundColor Red
    Write-Host "Open the app once, reproduce the capture problem, close it, and run this file again."
    exit 1
}

$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$desktop = [Environment]::GetFolderPath("Desktop")
$bundleRoot = Join-Path $desktop "AVE-Robot-Diagnostics-$stamp"
New-Item -ItemType Directory -Path $bundleRoot | Out-Null

$diagnosticsLog = Join-Path $runtimeRoot "logs\diagnostics.log"
$backendLog = Join-Path $runtimeRoot "logs\backend.log"
$settingsPath = Join-Path $runtimeRoot "data\settings.local.json"
$downloadsRoot = Join-Path $runtimeRoot "data\downloads"
$summaryPath = Join-Path $bundleRoot "summary.txt"
$eventPath = Join-Path $bundleRoot "robot-events.log"

$websocketUrl = "(not found)"
if (Test-Path -LiteralPath $settingsPath) {
    try {
        $settings = Get-Content -LiteralPath $settingsPath -Raw | ConvertFrom-Json
        $websocketUrl = Get-SafeWebSocketUrl ([string]$settings.robot.websocket_url)
    } catch {
        $websocketUrl = "(settings file could not be parsed)"
    }
}

$summary = New-Object System.Collections.Generic.List[string]
[void]$summary.Add("AVE robot diagnostics")
[void]$summary.Add("Collected: $((Get-Date).ToString('o'))")
[void]$summary.Add("Computer: $env:COMPUTERNAME")
[void]$summary.Add("Windows: $([System.Environment]::OSVersion.VersionString)")
[void]$summary.Add("Runtime root: $runtimeRoot")
[void]$summary.Add("Robot WebSocket URL: $websocketUrl")
[void]$summary.Add("")
[void]$summary.Add("Expected evidence sequence:")
[void]$summary.Add("  robot.command.sent")
[void]$summary.Add("  robot.reply.received (contains the robot-returned URL)")
[void]$summary.Add("  robot.media.download.started (contains the resolved HTTP(S) URL)")
[void]$summary.Add("  robot.media.download.completed (contains the Windows save path)")
[void]$summary.Add("  OR robot.media.download.failed / robot.reply.timeout")
[void]$summary.Add("")
[void]$summary.Add("Robot files currently saved under data\downloads:")

if (Test-Path -LiteralPath $downloadsRoot) {
    $downloads = @(Get-ChildItem -LiteralPath $downloadsRoot -File -Filter "robot-*" -ErrorAction SilentlyContinue |
        Sort-Object LastWriteTime -Descending)
    if ($downloads.Count -eq 0) {
        [void]$summary.Add("  (none)")
    } else {
        foreach ($file in $downloads) {
            [void]$summary.Add("  $($file.LastWriteTime.ToString('o'))  $($file.Length) bytes  $($file.FullName)")
        }
    }
} else {
    [void]$summary.Add("  (downloads directory does not exist)")
}

$summary | Set-Content -LiteralPath $summaryPath -Encoding UTF8

$patterns = @(
    "robot.websocket.",
    "robot.command.sent",
    "robot.reply.received",
    "robot.reply.timeout",
    "robot.media.download.",
    "capture.recording.",
    "capture.photo.",
    "gimbal.center.",
    "framing"
)
$escapedPattern = ($patterns | ForEach-Object { [regex]::Escape($_) }) -join "|"
$eventLines = New-Object System.Collections.Generic.List[string]

if (Test-Path -LiteralPath $diagnosticsLog) {
    Get-Content -LiteralPath $diagnosticsLog -Tail 5000 -ErrorAction SilentlyContinue |
        Where-Object { $_ -match $escapedPattern } |
        ForEach-Object { [void]$eventLines.Add($_) }
}

# Older builds may only have backend.log. Keep only AVE's structured, redacted lines.
if ($eventLines.Count -eq 0 -and (Test-Path -LiteralPath $backendLog)) {
    Get-Content -LiteralPath $backendLog -Tail 10000 -ErrorAction SilentlyContinue |
        Where-Object { $_ -match "\[ave\]" -and $_ -match $escapedPattern } |
        ForEach-Object { [void]$eventLines.Add($_) }
}

if ($eventLines.Count -eq 0) {
    [void]$eventLines.Add("No structured robot events were found. The installed app may predate diagnostics.log.")
}
$eventLines | Set-Content -LiteralPath $eventPath -Encoding UTF8

$zipPath = "$bundleRoot.zip"
Compress-Archive -LiteralPath $bundleRoot -DestinationPath $zipPath -Force

Write-Host "Done." -ForegroundColor Green
Write-Host "Send this file to support:"
Write-Host $zipPath -ForegroundColor Cyan
Write-Host ""
Write-Host "The ZIP contains only robot/capture event lines, the safe robot WebSocket address,"
Write-Host "and a file listing. It does not include API credentials or settings.local.json."
