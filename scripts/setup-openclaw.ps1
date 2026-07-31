param(
    [string]$OpenClawStateDir = "D:\OpenClawData",
    [string]$AgentId = "main"
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version 2.0

$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$pythonExe = Join-Path $projectRoot ".venv\Scripts\python.exe"
$skillSource = Join-Path $projectRoot "openclaw\agent-farm"
$envPath = Join-Path $projectRoot ".env"
$configPath = Join-Path $OpenClawStateDir "openclaw.json"

function Set-DotEnvValue {
    param(
        [string]$Path,
        [string]$Name,
        [string]$Value
    )

    $lines = if (Test-Path -LiteralPath $Path) {
        @(Get-Content -LiteralPath $Path)
    }
    else {
        @()
    }
    $updated = [System.Collections.Generic.List[string]]::new()
    $found = $false
    foreach ($line in $lines) {
        if ($line -match "^\s*$([Regex]::Escape($Name))=") {
            if (-not $found) {
                $updated.Add("$Name=$Value")
                $found = $true
            }
            continue
        }
        $updated.Add($line)
    }
    if (-not $found) {
        $updated.Add("$Name=$Value")
    }
    $updated | Set-Content -LiteralPath $Path -Encoding UTF8
}

function Get-DotEnvValue {
    param(
        [string]$Path,
        [string]$Name
    )

    if (-not (Test-Path -LiteralPath $Path)) {
        return ""
    }
    foreach ($line in Get-Content -LiteralPath $Path) {
        if ($line -match "^\s*$([Regex]::Escape($Name))=(.*)$") {
            return $Matches[1].Trim()
        }
    }
    return ""
}

if (-not (Test-Path -LiteralPath $pythonExe -PathType Leaf)) {
    throw "Backend Python was not found: $pythonExe"
}
if (-not (Test-Path -LiteralPath $skillSource -PathType Container)) {
    throw "OpenClaw Skill source was not found: $skillSource"
}
if (-not (Test-Path -LiteralPath $configPath -PathType Leaf)) {
    throw "OpenClaw config was not found: $configPath"
}

$openclaw = Get-Command openclaw -ErrorAction SilentlyContinue
if ($null -eq $openclaw) {
    throw "OpenClaw CLI was not found in PATH."
}

& $pythonExe -c "import mcp" 2>$null
if ($LASTEXITCODE -ne 0) {
    throw "Python MCP SDK is not installed. Run: .\.venv\Scripts\python.exe -m pip install -r requirements.txt"
}

$token = Get-DotEnvValue -Path $envPath -Name "OPENCLAW_FARM_TOKEN"
if (-not $token) {
    $bytes = New-Object byte[] 32
    [System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
    $token = ([BitConverter]::ToString($bytes)).Replace("-", "").ToLowerInvariant()
}

Set-DotEnvValue -Path $envPath -Name "CREDENTIAL_PROVIDER" -Value "mock"
Set-DotEnvValue -Path $envPath -Name "CMCC_ADMISSION_MODE" -Value "off"
Set-DotEnvValue -Path $envPath -Name "CMCC_ADMISSION_AGENT_MAPPINGS" -Value ""
Set-DotEnvValue -Path $envPath -Name "OPENCLAW_FARM_ENABLED" -Value "true"
Set-DotEnvValue -Path $envPath -Name "OPENCLAW_FARM_TOKEN" -Value $token
Set-DotEnvValue -Path $envPath -Name "OPENCLAW_FARM_RUN_TTL_SECONDS" -Value "300"
Set-DotEnvValue -Path $envPath -Name "OPENCLAW_MODEL_LABEL" -Value "deepseek/deepseek-v4-pro"

$timestamp = Get-Date -Format "yyyyMMdd-HHmmss"
$backupPath = "$configPath.backup-$timestamp"
Copy-Item -LiteralPath $configPath -Destination $backupPath

$env:OPENCLAW_STATE_DIR = $OpenClawStateDir

& $openclaw.Source skills install $skillSource `
    --agent $AgentId `
    --as "agent-farm" `
    --force
if ($LASTEXITCODE -ne 0) {
    throw "OpenClaw Skill installation failed. Config backup: $backupPath"
}

$serverConfig = [ordered]@{
    command = $pythonExe
    args = @("-m", "backend.openclaw_mcp.server")
    cwd = $projectRoot
    env = [ordered]@{
        FARM_API_BASE_URL = "http://127.0.0.1:8010"
        FARM_API_TOKEN = $token
        FARM_API_TIMEOUT_SECONDS = "10"
        PYTHONDONTWRITEBYTECODE = "1"
    }
    timeout = 15
}
$serverJson = $serverConfig | ConvertTo-Json -Depth 5 -Compress

& $openclaw.Source mcp set "agent-farm" $serverJson
if ($LASTEXITCODE -ne 0) {
    throw "OpenClaw MCP configuration failed. Config backup: $backupPath"
}

& $openclaw.Source config validate
if ($LASTEXITCODE -ne 0) {
    throw "OpenClaw config validation failed. Config backup: $backupPath"
}

& $openclaw.Source mcp reload
& $openclaw.Source mcp probe "agent-farm"
if ($LASTEXITCODE -ne 0) {
    throw "OpenClaw MCP probe failed. Config backup: $backupPath"
}

Write-Host ""
Write-Host "OpenClaw Agent Farm is ready."
Write-Host "State directory: $OpenClawStateDir"
Write-Host "Config backup: $backupPath"
Write-Host "Skill: agent-farm"
Write-Host "MCP server: agent-farm"
Write-Host "Farm API: http://127.0.0.1:8010"
