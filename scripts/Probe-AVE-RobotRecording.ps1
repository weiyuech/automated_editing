param(
    [string]$RobotWebSocketUrl = "",
    [ValidateRange(1, 15)]
    [int]$RecordSeconds = 3,
    [ValidateRange(10, 120)]
    [int]$ReplyTimeoutSeconds = 60,
    [switch]$Yes
)

# Standalone Windows robot recording probe.
# Requires only Windows PowerShell 5.1+; the editing app, Python, and extra packages are not used.
# It never sends set_goal or gimbal_control. The robot and lens are not moved.

$ErrorActionPreference = "Stop"
$script:Socket = $null
$script:StartWasSent = $false
$script:StopWasSent = $false
$script:BundleRoot = $null
$script:LogPath = $null
$script:SummaryPath = $null
$script:LastHeartbeat = $null

function Write-ProbeLog([string]$Message, [string]$Color = "Gray") {
    $line = "{0}  {1}" -f (Get-Date).ToString("o"), $Message
    if ($script:LogPath) {
        [System.IO.File]::AppendAllText(
            $script:LogPath,
            $line + [Environment]::NewLine,
            [System.Text.UTF8Encoding]::new($false)
        )
    }
    Write-Host $Message -ForegroundColor $Color
}

function Get-SafeUrl([string]$Value) {
    $trimmed = ([string]$Value).Trim()
    if ([string]::IsNullOrWhiteSpace($trimmed)) {
        return "(empty)"
    }
    $queryAt = $trimmed.IndexOf("?")
    if ($queryAt -ge 0) {
        return $trimmed.Substring(0, $queryAt) + "?<redacted>"
    }
    return $trimmed
}

function New-ByteSegment([byte[]]$Bytes) {
    # PowerShell 5.1 enumerates ArraySegment<T> when a function writes it to the pipeline.
    # The unary comma keeps it as one ArraySegment<byte>; otherwise SendAsync receives an
    # Object[] of individual bytes and rejects the command before anything reaches the robot.
    return ,([System.ArraySegment[byte]]::new($Bytes))
}

function Close-RobotSocket {
    if ($null -eq $script:Socket) {
        return
    }
    try {
        if ($script:Socket.State -eq [System.Net.WebSockets.WebSocketState]::Open) {
            $token = [System.Threading.CancellationToken]::None
            [void]$script:Socket.CloseAsync(
                [System.Net.WebSockets.WebSocketCloseStatus]::NormalClosure,
                "probe complete",
                $token
            ).GetAwaiter().GetResult()
        }
    } catch {
        # The diagnostic is already complete; a close-handshake failure is not material.
    } finally {
        $script:Socket.Dispose()
        $script:Socket = $null
    }
}

function Connect-Robot([System.Uri]$Uri, [int]$TimeoutSeconds = 15) {
    Close-RobotSocket
    $script:Socket = [System.Net.WebSockets.ClientWebSocket]::new()
    $timeout = [System.Threading.CancellationTokenSource]::new()
    $timeout.CancelAfter($TimeoutSeconds * 1000)
    try {
        Write-ProbeLog "Connecting to $(Get-SafeUrl $Uri.AbsoluteUri) ..." "Cyan"
        [void]$script:Socket.ConnectAsync($Uri, $timeout.Token).GetAwaiter().GetResult()
        Write-ProbeLog "WebSocket connected." "Green"
    } finally {
        $timeout.Dispose()
    }
}

function Send-RobotJson([hashtable]$Payload) {
    if ($null -eq $script:Socket -or
        $script:Socket.State -ne [System.Net.WebSockets.WebSocketState]::Open) {
        throw "Robot WebSocket is not open."
    }
    $json = $Payload | ConvertTo-Json -Depth 8 -Compress
    $bytes = [System.Text.Encoding]::UTF8.GetBytes($json)
    $segment = New-ByteSegment $bytes
    [void]$script:Socket.SendAsync(
        $segment,
        [System.Net.WebSockets.WebSocketMessageType]::Text,
        $true,
        [System.Threading.CancellationToken]::None
    ).GetAwaiter().GetResult()
    Write-ProbeLog "SENT $json"
}

function Receive-RobotText([int]$TimeoutMilliseconds) {
    $buffer = New-Object byte[] 16384
    $memory = [System.IO.MemoryStream]::new()
    $timeout = [System.Threading.CancellationTokenSource]::new()
    $timeout.CancelAfter([Math]::Max(1, $TimeoutMilliseconds))
    try {
        do {
            $segment = New-ByteSegment $buffer
            $result = $script:Socket.ReceiveAsync($segment, $timeout.Token).GetAwaiter().GetResult()
            if ($result.MessageType -eq [System.Net.WebSockets.WebSocketMessageType]::Close) {
                throw "Robot closed the WebSocket connection."
            }
            if ($result.Count -gt 0) {
                $memory.Write($buffer, 0, $result.Count)
            }
        } while (-not $result.EndOfMessage)
        return [System.Text.Encoding]::UTF8.GetString($memory.ToArray())
    } catch [System.OperationCanceledException] {
        throw "Timed out waiting for a robot WebSocket message."
    } finally {
        $timeout.Dispose()
        $memory.Dispose()
    }
}

function Write-HeartbeatSummary($Payload) {
    if ($null -eq $Payload.gimbal -and $null -eq $Payload.system -and $null -eq $Payload.task) {
        return
    }
    $script:LastHeartbeat = $Payload
    $parts = New-Object System.Collections.Generic.List[string]
    if ($null -ne $Payload.system) {
        [void]$parts.Add("system=$($Payload.system.status)")
    }
    if ($null -ne $Payload.gimbal) {
        [void]$parts.Add("record_status=$($Payload.gimbal.record_status)")
        [void]$parts.Add("yaw=$($Payload.gimbal.yaw)")
        [void]$parts.Add("pitch=$($Payload.gimbal.pitch)")
    }
    if ($null -ne $Payload.task) {
        [void]$parts.Add("goal_status=$($Payload.task.goal_status)")
        [void]$parts.Add("object_status=$($Payload.task.object_status)")
    }
    Write-ProbeLog ("HEARTBEAT " + ($parts -join " "))
}

function Wait-VideoReply([string]$Phase, [int]$TimeoutSeconds) {
    $deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
    while ([DateTime]::UtcNow -lt $deadline) {
        $remaining = [int][Math]::Ceiling(($deadline - [DateTime]::UtcNow).TotalMilliseconds)
        $raw = Receive-RobotText $remaining
        try {
            $payload = $raw | ConvertFrom-Json
        } catch {
            Write-ProbeLog "RECEIVED non-JSON message ($($raw.Length) characters)." "Yellow"
            continue
        }

        Write-HeartbeatSummary $payload
        if ($null -eq $payload.robot_video_record) {
            $keys = @($payload.PSObject.Properties.Name) -join ","
            if ($keys -and $null -eq $payload.gimbal -and $null -eq $payload.system) {
                Write-ProbeLog "RECEIVED unrelated JSON keys: $keys"
            }
            continue
        }

        $reply = $payload.robot_video_record
        $safeUrl = Get-SafeUrl ([string]$reply.url)
        Write-ProbeLog (
            "REPLY robot_video_record phase=$Phase status=$($reply.status) " +
            "start=$($reply.start) stop=$($reply.stop) resolution=$($reply.resolution) url=$safeUrl"
        ) "Cyan"

        $replyFields = @($reply.PSObject.Properties.Name)
        $hasStart = $replyFields -contains "start"
        $hasStop = $replyFields -contains "stop"
        if ($Phase -eq "start" -and $hasStop -and -not $hasStart) {
            Write-ProbeLog "Ignoring an explicit stop reply while waiting for start." "Yellow"
            continue
        }
        if ($Phase -eq "stop" -and $hasStart -and -not $hasStop) {
            Write-ProbeLog "Ignoring a delayed explicit start reply while waiting for stop." "Yellow"
            continue
        }
        return $reply
    }
    throw "Timed out after $TimeoutSeconds seconds waiting for robot_video_record during $Phase."
}

function Get-MediaCandidates([string]$RawUrl, [System.Uri]$WebSocketUri) {
    $value = ([string]$RawUrl).Trim()
    if ([string]::IsNullOrWhiteSpace($value)) {
        throw "The robot response did not contain a media URL."
    }
    if ($value -match '(?i)^file:' -or
        $value -match '^[A-Za-z]:[\\/]' -or
        $value.StartsWith("\\") -or
        $value -match '^/(home|tmp|var|mnt|Users)/') {
        throw "The robot returned its own filesystem path, not a Windows-downloadable URL: $value"
    }

    $candidates = New-Object System.Collections.Generic.List[string]
    if ($value -match '(?i)^https?://') {
        [void]$candidates.Add($value)
        return $candidates
    }
    if ($value -match '^[A-Za-z][A-Za-z0-9+.-]*:') {
        throw "Unsupported media URL scheme returned by robot: $value"
    }

    $downloadScheme = if ($WebSocketUri.Scheme -eq "wss") { "https" } else { "http" }
    if ($value -match '^(\[[0-9A-Fa-f:]+\]|[^/:\s]+):\d+(/|$)' -or
        $value -match '^(\d{1,3}\.){3}\d{1,3}(/|$)') {
        [void]$candidates.Add("${downloadScheme}://$value")
        return $candidates
    }

    # The protocol does not define the HTTP media port. Try the WebSocket authority first,
    # because that matches the app, then the standard HTTP(S) port as a diagnostic fallback.
    $samePortBase = [System.Uri]::new("${downloadScheme}://$($WebSocketUri.Authority)/")
    [void]$candidates.Add(([System.Uri]::new($samePortBase, $value)).AbsoluteUri)

    $hostForUrl = $WebSocketUri.Host
    if ($hostForUrl.Contains(":")) {
        $hostForUrl = "[$hostForUrl]"
    }
    $defaultPortBase = [System.Uri]::new("${downloadScheme}://${hostForUrl}/")
    $defaultCandidate = ([System.Uri]::new($defaultPortBase, $value)).AbsoluteUri
    if (-not $candidates.Contains($defaultCandidate)) {
        [void]$candidates.Add($defaultCandidate)
    }
    return $candidates
}

function Get-DownloadExtension([string]$Url) {
    try {
        $extension = [System.IO.Path]::GetExtension(([System.Uri]$Url).AbsolutePath).ToLowerInvariant()
    } catch {
        $extension = ""
    }
    if ($extension -notin @(".mp4", ".mov", ".m4v", ".avi", ".mkv", ".webm")) {
        return ".mp4"
    }
    return $extension
}

function Download-MediaCandidates($Candidates) {
    foreach ($candidate in $Candidates) {
        $safeCandidate = Get-SafeUrl $candidate
        $target = Join-Path $script:BundleRoot ("fetched-video" + (Get-DownloadExtension $candidate))
        Write-ProbeLog "DOWNLOAD trying $safeCandidate" "Cyan"
        try {
            $request = @{
                Uri = $candidate
                UseBasicParsing = $true
                OutFile = $target
                TimeoutSec = 90
                MaximumRedirection = 5
            }
            Invoke-WebRequest @request
            $file = Get-Item -LiteralPath $target
            if ($file.Length -le 0) {
                throw "The HTTP response created an empty file."
            }
            Write-ProbeLog "DOWNLOAD OK bytes=$($file.Length) saved=$($file.FullName)" "Green"
            return $file
        } catch {
            Remove-Item -LiteralPath $target -Force -ErrorAction SilentlyContinue
            Write-ProbeLog "DOWNLOAD FAILED url=$safeCandidate error=$($_.Exception.Message)" "Yellow"
        }
    }
    return $null
}

function Send-SafetyStop([System.Uri]$Uri) {
    if (-not $script:StartWasSent -or $script:StopWasSent) {
        return
    }
    Write-ProbeLog "Safety cleanup: attempting video_record stop." "Yellow"
    try {
        if ($null -eq $script:Socket -or
            $script:Socket.State -ne [System.Net.WebSockets.WebSocketState]::Open) {
            Connect-Robot $Uri 10
        }
        Send-RobotJson @{ video_record = @{ stop = 0 } }
        $script:StopWasSent = $true
        Write-ProbeLog "Safety stop command sent." "Green"
    } catch {
        Write-ProbeLog "SAFETY STOP FAILED: $($_.Exception.Message)" "Red"
    }
}

if ([string]::IsNullOrWhiteSpace($RobotWebSocketUrl)) {
    $RobotWebSocketUrl = Read-Host "Robot WebSocket URL (example: ws://10.73.2.199:8765)"
}

try {
    $robotUri = [System.Uri]$RobotWebSocketUrl
} catch {
    Write-Host "Invalid WebSocket URL: $RobotWebSocketUrl" -ForegroundColor Red
    exit 2
}
if ($robotUri.Scheme -notin @("ws", "wss") -or [string]::IsNullOrWhiteSpace($robotUri.Host)) {
    Write-Host "The robot address must start with ws:// or wss:// and include a host." -ForegroundColor Red
    exit 2
}

Write-Host ""
Write-Host "This standalone test will:" -ForegroundColor Cyan
Write-Host "  1. Connect directly to the robot (the editing app is not used)."
Write-Host "  2. Record $RecordSeconds seconds at the CURRENT lens angle."
Write-Host "  3. Stop recording, display the returned URL, and try to download it."
Write-Host "It will NOT move the robot or gimbal."
Write-Host ""
if (-not $Yes) {
    $confirmation = Read-Host "Type RECORD to continue"
    if ($confirmation -ne "RECORD") {
        Write-Host "Canceled. No command was sent."
        exit 0
    }
}

$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$desktop = [Environment]::GetFolderPath("Desktop")
if ([string]::IsNullOrWhiteSpace($desktop)) {
    $desktop = Join-Path $env:USERPROFILE "Desktop"
}
$script:BundleRoot = Join-Path $desktop "AVE-Standalone-Robot-Probe-$stamp"
New-Item -ItemType Directory -Path $script:BundleRoot | Out-Null
$script:LogPath = Join-Path $script:BundleRoot "robot-recording-probe.log"
$script:SummaryPath = Join-Path $script:BundleRoot "summary.txt"
[System.IO.File]::WriteAllText($script:LogPath, "", [System.Text.UTF8Encoding]::new($false))

$startReply = $null
$stopReply = $null
$mediaUrl = ""
$downloadedFile = $null
$downloadedSha256 = ""
$outcome = "failed"
$failure = ""

try {
    Write-ProbeLog "Standalone probe started. No AVE installation is used." "Cyan"
    Write-ProbeLog "Robot WebSocket: $(Get-SafeUrl $robotUri.AbsoluteUri)"
    Write-ProbeLog "No set_goal or gimbal_control command will be sent."
    Connect-Robot $robotUri 15

    Send-RobotJson @{ video_record = @{ start = 0; resolution = 4 } }
    $script:StartWasSent = $true
    $startReply = Wait-VideoReply "start" 15
    if ([string]$startReply.status -ne "ok") {
        throw "Robot rejected recording start: status=$($startReply.status)"
    }

    Write-ProbeLog "Recording for $RecordSeconds seconds at the current lens angle ..." "Cyan"
    Start-Sleep -Seconds $RecordSeconds

    Send-RobotJson @{ video_record = @{ stop = 0 } }
    $script:StopWasSent = $true
    $stopReply = Wait-VideoReply "stop" $ReplyTimeoutSeconds
    if ([string]$stopReply.status -ne "ok") {
        throw "Robot rejected recording stop: status=$($stopReply.status)"
    }

    $mediaUrl = [string]$stopReply.url
    if ([string]::IsNullOrWhiteSpace($mediaUrl)) {
        $mediaUrl = [string]$startReply.url
        if (-not [string]::IsNullOrWhiteSpace($mediaUrl)) {
            Write-ProbeLog "Stop reply had no URL; using the URL from the start reply." "Yellow"
        }
    }
    if ([string]::IsNullOrWhiteSpace($mediaUrl)) {
        throw "Robot reported success but returned no media URL on start or stop."
    }

    Write-ProbeLog "ROBOT RETURNED URL: $(Get-SafeUrl $mediaUrl)" "Cyan"
    $candidates = Get-MediaCandidates $mediaUrl $robotUri
    foreach ($candidate in $candidates) {
        Write-ProbeLog "RESOLVED CANDIDATE: $(Get-SafeUrl $candidate)"
    }
    $downloadedFile = Download-MediaCandidates $candidates
    if ($null -eq $downloadedFile) {
        throw "The robot returned a URL, but Windows could not download it using any resolved candidate."
    }
    $downloadedSha256 = (Get-FileHash -LiteralPath $downloadedFile.FullName -Algorithm SHA256).Hash
    Write-ProbeLog "DOWNLOAD SHA256=$downloadedSha256"
    $outcome = "success"
} catch {
    $failure = $_.Exception.Message
    Write-ProbeLog "PROBE FAILED: $failure" "Red"
} finally {
    Send-SafetyStop $robotUri
    Close-RobotSocket
}

$summary = New-Object System.Collections.Generic.List[string]
[void]$summary.Add("AVE standalone robot recording probe")
[void]$summary.Add("Collected: $((Get-Date).ToString('o'))")
[void]$summary.Add("Outcome: $outcome")
[void]$summary.Add("Computer: $env:COMPUTERNAME")
[void]$summary.Add("Windows: $([Environment]::OSVersion.VersionString)")
[void]$summary.Add("PowerShell: $($PSVersionTable.PSVersion)")
[void]$summary.Add("Robot WebSocket: $(Get-SafeUrl $robotUri.AbsoluteUri)")
[void]$summary.Add("Record seconds: $RecordSeconds")
[void]$summary.Add("Start status: $($startReply.status)")
[void]$summary.Add("Stop status: $($stopReply.status)")
[void]$summary.Add("Robot returned URL: $(Get-SafeUrl $mediaUrl)")
[void]$summary.Add("Downloaded file: $(if ($downloadedFile) { $downloadedFile.FullName } else { '(none)' })")
[void]$summary.Add("Downloaded bytes: $(if ($downloadedFile) { $downloadedFile.Length } else { 0 })")
[void]$summary.Add("Downloaded SHA256: $downloadedSha256")
[void]$summary.Add("Failure: $failure")
[void]$summary.Add("")
[void]$summary.Add("This probe did not use the editing app and did not send movement or gimbal commands.")
$summary | Set-Content -LiteralPath $script:SummaryPath -Encoding UTF8

$zipPath = "$($script:BundleRoot).zip"
try {
    # Keep the support attachment small. A successfully fetched video remains beside these
    # files in BundleRoot and is represented in the ZIP by its size and SHA-256 digest.
    Compress-Archive -LiteralPath @($script:LogPath, $script:SummaryPath) -DestinationPath $zipPath -Force
    Write-Host ""
    Write-Host "Send this diagnostic ZIP:" -ForegroundColor Cyan
    Write-Host $zipPath -ForegroundColor Cyan
} catch {
    Write-Host "Could not create ZIP; send this folder instead:" -ForegroundColor Yellow
    Write-Host $script:BundleRoot -ForegroundColor Yellow
}

if ($outcome -eq "success") {
    Write-Host "Robot recording and Windows download both succeeded." -ForegroundColor Green
    exit 0
}
Write-Host "The probe captured the failure details in the ZIP." -ForegroundColor Red
exit 1
