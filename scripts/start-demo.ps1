$ErrorActionPreference = "Stop"
Set-StrictMode -Version 2.0

$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$backendPython = Join-Path $projectRoot ".venv\Scripts\python.exe"
$bundledNode = "C:\Users\admin\.cache\codex-runtimes\codex-primary-runtime\dependencies\node\bin\node.exe"
$vinextCli = Join-Path $projectRoot "node_modules\vinext\dist\cli.js"
$logDir = Join-Path $projectRoot ".logs"
$manifestPath = Join-Path $logDir "demo-processes.json"
$manifestTempPath = "$manifestPath.tmp"

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

function Test-ManifestRecordIsCurrent {
    param($Record)

    $process = Get-Process -Id ([int]$Record.pid) -ErrorAction SilentlyContinue
    if ($null -eq $process) {
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
        $actualStart = $process.StartTime.ToUniversalTime()
        return [Math]::Abs(($actualStart - $recordedStart).TotalSeconds) -le 3
    }
    catch {
        return $false
    }
}

if (-not (Test-Path -LiteralPath $backendPython -PathType Leaf)) {
    throw "Backend Python was not found: $backendPython"
}

if (-not (Test-Path -LiteralPath $vinextCli -PathType Leaf)) {
    throw "Vinext CLI was not found: $vinextCli. Run npm install first."
}

$nodeExe = $bundledNode
if (-not (Test-Path -LiteralPath $nodeExe -PathType Leaf)) {
    $nodeCommand = Get-Command node.exe -ErrorAction SilentlyContinue
    if ($null -eq $nodeCommand) {
        throw "Node.js was not found. Install Node.js 22.13 or newer."
    }
    $nodeExe = $nodeCommand.Source
}

New-Item -ItemType Directory -Force -Path $logDir | Out-Null

if (Test-Path -LiteralPath $manifestPath -PathType Leaf) {
    try {
        $existingManifest = Get-Content -LiteralPath $manifestPath -Raw |
            ConvertFrom-Json
        $runningRecords = @(
            $existingManifest.processes |
                Where-Object { Test-ManifestRecordIsCurrent $_ }
        )
        if ($runningRecords.Count -gt 0) {
            $runningRoles = ($runningRecords.role | Sort-Object -Unique) -join ", "
            throw "Demo processes are already running: $runningRoles. Run scripts\stop-demo.ps1 first."
        }
    }
    catch {
        if ($_.Exception.Message -like "Demo processes are already running:*") {
            throw
        }
        Write-Warning "Ignoring a stale or unreadable process manifest."
    }

    Remove-Item -LiteralPath $manifestPath -Force -ErrorAction SilentlyContinue
}

$occupiedPorts = @()
foreach ($port in @(3000, 8010)) {
    $listeners = @(Get-ListeningProcessIds -Port $port)
    if ($listeners.Count -gt 0) {
        $occupiedPorts += "$port (PID $($listeners -join ', '))"
    }
}

if ($occupiedPorts.Count -gt 0) {
    throw "Required ports are already in use: $($occupiedPorts -join '; '). Stop the existing process before starting the demo."
}

$startedProcesses = @()
try {
    $backendProcess = Start-Process `
        -FilePath $backendPython `
        -ArgumentList @(
            "-m",
            "uvicorn",
            "backend.app.main:app",
            "--host",
            "127.0.0.1",
            "--port",
            "8010",
            "--reload"
        ) `
        -WorkingDirectory $projectRoot `
        -WindowStyle Hidden `
        -RedirectStandardOutput (Join-Path $logDir "backend.out.log") `
        -RedirectStandardError (Join-Path $logDir "backend.err.log") `
        -PassThru
    $startedProcesses += $backendProcess

    $frontendProcess = Start-Process `
        -FilePath $nodeExe `
        -ArgumentList @($vinextCli, "dev") `
        -WorkingDirectory $projectRoot `
        -WindowStyle Hidden `
        -RedirectStandardOutput (Join-Path $logDir "frontend.out.log") `
        -RedirectStandardError (Join-Path $logDir "frontend.err.log") `
        -PassThru
    $startedProcesses += $frontendProcess

    Start-Sleep -Milliseconds 750
    foreach ($process in $startedProcesses) {
        $process.Refresh()
        if ($process.HasExited) {
            throw "A demo process exited during startup. Check the files in $logDir."
        }
    }

    $manifest = [ordered]@{
        schemaVersion = 1
        projectRoot = $projectRoot
        createdAt = [DateTime]::UtcNow.ToString("o")
        processes = @(
            [ordered]@{
                role = "backend"
                pid = $backendProcess.Id
                port = 8010
                executable = $backendPython
                commandToken = "backend.app.main:app"
                startedAt = $backendProcess.StartTime.ToUniversalTime().ToString("o")
            },
            [ordered]@{
                role = "frontend"
                pid = $frontendProcess.Id
                port = 3000
                executable = $nodeExe
                commandToken = "vinext"
                startedAt = $frontendProcess.StartTime.ToUniversalTime().ToString("o")
            }
        )
    }

    $manifest | ConvertTo-Json -Depth 4 |
        Set-Content -LiteralPath $manifestTempPath -Encoding UTF8
    Move-Item -LiteralPath $manifestTempPath -Destination $manifestPath -Force
}
catch {
    foreach ($process in $startedProcesses) {
        if ($null -ne $process -and -not $process.HasExited) {
            Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue
        }
    }
    Remove-Item -LiteralPath $manifestTempPath -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $manifestPath -Force -ErrorAction SilentlyContinue
    throw
}

Write-Output "Farm Demo: http://localhost:3000 (PID $($frontendProcess.Id))"
Write-Output "FastAPI docs: http://127.0.0.1:8010/docs (PID $($backendProcess.Id))"
Write-Output "Process manifest: $manifestPath"
