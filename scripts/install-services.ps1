# Windows Service Setup for DeejAI Host Processes
#
# Run these scripts from an elevated PowerShell (Run as Administrator).
# Requires NSSM: https://nssm.cc/download (extract nssm.exe to C:\Tools\ or similar)
#
# Usage:
#   .\install-services.ps1          — install both services
#   .\install-services.ps1 -Remove  — remove both services

param(
    [switch]$Remove,
    [string]$NssmPath = "nssm",
    [string]$PythonPath = "C:\Dev\DeeJAI\venv\Scripts\python.exe",
    [string]$DeejaiRoot = "C:\Dev\DeeJAI"
)

$ErrorActionPreference = "Stop"

function Remove-ServiceIfExists($name) {
    $svc = Get-Service -Name $name -ErrorAction SilentlyContinue
    if ($svc) {
        Write-Host "Removing service: $name"
        & $NssmPath stop $name 2>$null
        & $NssmPath remove $name confirm 2>$null
    }
}

if ($Remove) {
    Remove-ServiceIfExists "DeejAI-Station"
    Remove-ServiceIfExists "DeejAI-Importer"
    Write-Host "Services removed."
    exit 0
}

# ── Station Server (VLC local radio) ──────────────────────────────────────

Write-Host "Installing DeejAI-Station service..."
& $NssmPath install DeejAI-Station $PythonPath
& $NssmPath set DeejAI-Station AppParameters "$DeejaiRoot\radio\station_server.py"
& $NssmPath set DeejAI-Station AppDirectory $DeejaiRoot
& $NssmPath set DeejAI-Station DisplayName "DeejAI Station Server"
& $NssmPath set DeejAI-Station Description "Local VLC radio server (port 8051)"
& $NssmPath set DeejAI-Station Start SERVICE_AUTO_START
& $NssmPath set DeejAI-Station AppStdout "$DeejaiRoot\logs\station-stdout.log"
& $NssmPath set DeejAI-Station AppStderr "$DeejaiRoot\logs\station-stderr.log"
& $NssmPath set DeejAI-Station AppRotateFiles 1
& $NssmPath set DeejAI-Station AppRotateBytes 10485760
& $NssmPath set DeejAI-Station AppEnvironmentExtra "PYTHONUNBUFFERED=1"

# ── Import Watcher (inbox → beets → embed → reload) ──────────────────────

Write-Host "Installing DeejAI-Importer service..."
& $NssmPath install DeejAI-Importer $PythonPath
& $NssmPath set DeejAI-Importer AppParameters "$DeejaiRoot\import_pipeline\watcher.py"
& $NssmPath set DeejAI-Importer AppDirectory $DeejaiRoot
& $NssmPath set DeejAI-Importer DisplayName "DeejAI Import Watcher"
& $NssmPath set DeejAI-Importer Description "Monitors MusicInbox for new files, runs beets + embedding pipeline"
& $NssmPath set DeejAI-Importer Start SERVICE_AUTO_START
& $NssmPath set DeejAI-Importer AppStdout "$DeejaiRoot\logs\importer-stdout.log"
& $NssmPath set DeejAI-Importer AppStderr "$DeejaiRoot\logs\importer-stderr.log"
& $NssmPath set DeejAI-Importer AppRotateFiles 1
& $NssmPath set DeejAI-Importer AppRotateBytes 10485760
& $NssmPath set DeejAI-Importer AppEnvironmentExtra "PYTHONUNBUFFERED=1"

# Create logs directory
New-Item -ItemType Directory -Force -Path "$DeejaiRoot\logs" | Out-Null

Write-Host ""
Write-Host "Services installed. To start them:"
Write-Host "  nssm start DeejAI-Station"
Write-Host "  nssm start DeejAI-Importer"
Write-Host ""
Write-Host "Note: DeejAI-Importer requires watcher.py to exist in import_pipeline/"
Write-Host "Note: DeejAI-Station is optional if Navidrome handles streaming."
