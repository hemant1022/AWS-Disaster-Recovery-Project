# Load .env into the current PowerShell session.
#
#   . .\scripts\env.ps1
#
# Dot-source it (note the leading dot) so the variables land in your shell.
# Ignore lines that are blank or start with '#'. Quotes around values are stripped.

$ErrorActionPreference = 'Stop'

$repoRoot = Split-Path -Parent $PSScriptRoot
$envFile = Join-Path $repoRoot '.env'

if (-not (Test-Path $envFile)) {
    throw "Missing $envFile. Copy .env.example to .env and fill it in."
}

$loaded = 0
foreach ($raw in Get-Content $envFile) {
    $line = $raw.Trim()
    if ($line -eq '' -or $line.StartsWith('#')) { continue }

    $split = $line.IndexOf('=')
    if ($split -lt 1) { continue }

    $name = $line.Substring(0, $split).Trim()
    $value = $line.Substring($split + 1).Trim().Trim('"').Trim("'")

    Set-Item -Path "Env:$name" -Value $value
    $loaded++
}

Write-Host "Loaded $loaded variables from $envFile"
