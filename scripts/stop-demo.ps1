param(
    [ValidateRange(1, 30)]
    [int]$GracePeriodSeconds = 5
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version 2.0

$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$projectPrefix = $projectRoot.TrimEnd("\") + "\"
$vinextCli = Join-Path $projectRoot "node_modules\vinext\dist\cli.js"
$manifestPath = Join-Path $projectRoot ".logs\demo-processes.json"
$rolePorts = [ordered]@{
    frontend = 3000
    backend = 8010
}

$script:processIndex = @{}

function Update-ProcessSnapshot {
    $script:processIndex = @{}
    foreach ($processInfo in @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue)) {
        $script:processIndex[[int]$processInfo.ProcessId] = $processInfo
    }
}

function Get-ProcessInfo {
    param([int]$ProcessId)

    if ($script:processIndex.ContainsKey($ProcessId)) {
        return $script:processIndex[$ProcessId]
    }
    return $null
}

function Get-ListeningProcessIds {
    param([int]$Port)

    $processIds = @()
    try {
        $processIds = @(
            Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction Stop |
                Select-Object -ExpandProperty OwningProcess
        )
    }
    catch {
        $pattern = "^\s*TCP\s+\S+:$Port\s+\S+\s+LISTENING\s+(\d+)\s*$"
        foreach ($line in (& netstat -ano -p TCP)) {
            if ($line -match $pattern) {
                $processIds += [int]$Matches[1]
            }
        }
    }

    return @($processIds | Sort-Object -Unique)
}

function Test-ProjectLocation {
    param($ProcessInfo)

    $commandLine = [string]$ProcessInfo.CommandLine
    $executablePath = [string]$ProcessInfo.ExecutablePath

    if ($commandLine.IndexOf($projectRoot, [StringComparison]::OrdinalIgnoreCase) -ge 0) {
        return $true
    }

    if (
        $executablePath.StartsWith(
            $projectPrefix,
            [StringComparison]::OrdinalIgnoreCase
        )
    ) {
        return $true
    }

    return $false
}

function Test-RoleSignature {
    param(
        $ProcessInfo,
        [ValidateSet("frontend", "backend")]
        [string]$Role
    )

    if ($null -eq $ProcessInfo -or -not (Test-ProjectLocation $ProcessInfo)) {
        return $false
    }

    $commandLine = ([string]$ProcessInfo.CommandLine).ToLowerInvariant()
    if ($Role -eq "frontend") {
        return (
            $commandLine.Contains("vinext") -and
            (
                $commandLine.Contains($vinextCli.ToLowerInvariant()) -or
                $commandLine.Contains(" dev")
            )
        )
    }

    return (
        $commandLine.Contains("uvicorn") -and
        $commandLine.Contains("backend.app.main:app")
    )
}

function Test-ManifestRecord {
    param($Record)

    $processId = [int]$Record.pid
    $processInfo = Get-ProcessInfo -ProcessId $processId
    if ($null -eq $processInfo) {
        return $false
    }

    $role = [string]$Record.role
    if (-not (Test-RoleSignature -ProcessInfo $processInfo -Role $role)) {
        return $false
    }

    try {
        if ($Record.startedAt -is [DateTime]) {
            $recordedStart = ([DateTime]$Record.startedAt).ToUniversalTime()
        }
        else {
            $recordedStart = [DateTimeOffset]::Parse(
                [string]$Record.startedAt
            ).UtcDateTime
        }
        $actualProcess = Get-Process -Id $processId -ErrorAction Stop
        $actualStart = $actualProcess.StartTime.ToUniversalTime()
        if ([Math]::Abs(($actualStart - $recordedStart).TotalSeconds) -gt 3) {
            return $false
        }
    }
    catch {
        return $false
    }

    $recordedExecutable = [string]$Record.executable
    $actualExecutable = [string]$processInfo.ExecutablePath
    if (
        $recordedExecutable -and
        $actualExecutable -and
        -not $actualExecutable.Equals(
            $recordedExecutable,
            [StringComparison]::OrdinalIgnoreCase
        )
    ) {
        return $false
    }

    return $true
}

function Find-VerifiedRoleRoot {
    param(
        [int]$ProcessId,
        [ValidateSet("frontend", "backend")]
        [string]$Role
    )

    $visited = @{}
    $currentId = $ProcessId
    while ($currentId -gt 0 -and -not $visited.ContainsKey($currentId)) {
        $visited[$currentId] = $true
        $processInfo = Get-ProcessInfo -ProcessId $currentId
        if ($null -eq $processInfo) {
            return $null
        }

        if (Test-RoleSignature -ProcessInfo $processInfo -Role $Role) {
            return $currentId
        }

        $currentId = [int]$processInfo.ParentProcessId
    }

    return $null
}

function Get-TargetProcessTree {
    param($RootTargets)

    $childrenByParent = @{}
    foreach ($processInfo in $script:processIndex.Values) {
        $parentId = [int]$processInfo.ParentProcessId
        if (-not $childrenByParent.ContainsKey($parentId)) {
            $childrenByParent[$parentId] = @()
        }
        $childrenByParent[$parentId] += [int]$processInfo.ProcessId
    }

    $targets = @{}
    $queue = New-Object System.Collections.Queue
    foreach ($entry in $RootTargets.GetEnumerator()) {
        $queue.Enqueue(
            [pscustomobject]@{
                ProcessId = [int]$entry.Key
                Role = [string]$entry.Value
                Depth = 0
            }
        )
    }

    while ($queue.Count -gt 0) {
        $item = $queue.Dequeue()
        if (
            $targets.ContainsKey($item.ProcessId) -and
            $targets[$item.ProcessId].Depth -ge $item.Depth
        ) {
            continue
        }

        $targets[$item.ProcessId] = $item
        if ($childrenByParent.ContainsKey($item.ProcessId)) {
            foreach ($childId in $childrenByParent[$item.ProcessId]) {
                $queue.Enqueue(
                    [pscustomobject]@{
                        ProcessId = [int]$childId
                        Role = $item.Role
                        Depth = $item.Depth + 1
                    }
                )
            }
        }
    }

    return @($targets.Values | Sort-Object Depth -Descending)
}

function Get-LiveProcessIds {
    param([int[]]$ProcessIds)

    return @(
        $ProcessIds |
            Where-Object {
                $null -ne (Get-Process -Id $_ -ErrorAction SilentlyContinue)
            }
    )
}

Update-ProcessSnapshot

$rootTargets = @{}
$manifestWasRead = $false
if (Test-Path -LiteralPath $manifestPath -PathType Leaf) {
    try {
        $manifest = Get-Content -LiteralPath $manifestPath -Raw |
            ConvertFrom-Json
        $manifestRoot = [string]$manifest.projectRoot
        if (
            -not $manifestRoot.Equals(
                $projectRoot,
                [StringComparison]::OrdinalIgnoreCase
            )
        ) {
            Write-Warning "The process manifest belongs to a different project path."
        }
        else {
            $manifestWasRead = $true
            foreach ($record in @($manifest.processes)) {
                if (Test-ManifestRecord -Record $record) {
                    $rootTargets[[int]$record.pid] = [string]$record.role
                }
                else {
                    Write-Warning "Ignoring stale process record for $($record.role) PID $($record.pid)."
                }
            }
        }
    }
    catch {
        Write-Warning "The process manifest is unreadable; using port discovery."
    }
}

foreach ($role in $rolePorts.Keys) {
    $port = [int]$rolePorts[$role]
    foreach ($listenerId in @(Get-ListeningProcessIds -Port $port)) {
        $verifiedRoot = Find-VerifiedRoleRoot -ProcessId $listenerId -Role $role
        if ($null -ne $verifiedRoot) {
            $rootTargets[[int]$verifiedRoot] = $role
        }
        else {
            Write-Warning "Port $port is used by unverified PID $listenerId; it will not be stopped."
        }
    }
}

if ($rootTargets.Count -eq 0) {
    if ($manifestWasRead -or (Test-Path -LiteralPath $manifestPath)) {
        Remove-Item -LiteralPath $manifestPath -Force -ErrorAction SilentlyContinue
    }
    Write-Output "Demo is not running. No project process was stopped."
    return
}

$targetTree = @(Get-TargetProcessTree -RootTargets $rootTargets)
$targetIds = @($targetTree | Select-Object -ExpandProperty ProcessId)
$rolesFound = @($targetTree.Role | Sort-Object -Unique)

Write-Output "Stopping demo roles: $($rolesFound -join ', ')"
foreach ($target in $targetTree) {
    if ($null -ne (Get-Process -Id $target.ProcessId -ErrorAction SilentlyContinue)) {
        Stop-Process -Id $target.ProcessId -ErrorAction SilentlyContinue
    }
}

$deadline = [DateTime]::UtcNow.AddSeconds($GracePeriodSeconds)
$remainingIds = @(Get-LiveProcessIds -ProcessIds $targetIds)
while ($remainingIds.Count -gt 0 -and [DateTime]::UtcNow -lt $deadline) {
    Start-Sleep -Milliseconds 200
    $remainingIds = @(Get-LiveProcessIds -ProcessIds $targetIds)
}

if ($remainingIds.Count -gt 0) {
    Write-Warning "Forcing remaining verified processes: $($remainingIds -join ', ')"
    foreach ($processId in $remainingIds) {
        Stop-Process -Id $processId -Force -ErrorAction SilentlyContinue
    }
    Start-Sleep -Milliseconds 500
}

$remainingIds = @(Get-LiveProcessIds -ProcessIds $targetIds)
Update-ProcessSnapshot

$projectListenersRemain = $false
foreach ($role in $rolePorts.Keys) {
    $port = [int]$rolePorts[$role]
    $listeners = @(Get-ListeningProcessIds -Port $port)
    $verifiedListeners = @()
    $unknownListeners = @()

    foreach ($listenerId in $listeners) {
        $verifiedRoot = Find-VerifiedRoleRoot -ProcessId $listenerId -Role $role
        if ($null -ne $verifiedRoot) {
            $verifiedListeners += $listenerId
        }
        else {
            $unknownListeners += $listenerId
        }
    }

    if ($verifiedListeners.Count -gt 0) {
        $projectListenersRemain = $true
        Write-Warning "$role still owns port $port through PID $($verifiedListeners -join ', ')."
    }
    elseif ($unknownListeners.Count -gt 0) {
        Write-Warning "$role stopped, but unrelated PID $($unknownListeners -join ', ') still owns port $port."
    }
    elseif ($rolesFound -contains $role) {
        Write-Output "$role stopped; port $port is free."
    }
    else {
        Write-Output "$role was not running; port $port is free."
    }
}

if ($remainingIds.Count -eq 0 -and -not $projectListenersRemain) {
    Remove-Item -LiteralPath $manifestPath -Force -ErrorAction SilentlyContinue
    Write-Output "Demo stopped successfully. Logs and SQLite data were preserved."
    return
}

throw "Some verified demo processes are still running: $($remainingIds -join ', ')"
