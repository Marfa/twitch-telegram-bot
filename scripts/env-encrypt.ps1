# Encrypt .env and remind to move the private key into Bitwarden.
# Usage: .\scripts\env-encrypt.ps1
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $Root

if (-not (Get-Command dotenvx -ErrorAction SilentlyContinue)) {
    Write-Error "dotenvx not found. Install: winget install dotenvx.dotenvx"
    exit 1
}
if (-not (Test-Path .env)) {
    Write-Error "Missing .env — copy from .env.example and fill values first."
    exit 1
}

& dotenvx encrypt
Write-Host ""
Write-Host "Next: store the private key in Bitwarden (removes it from .env.keys):"
Write-Host "  bw login"
Write-Host "  dotenvx bitwarden up"
Write-Host ""
Write-Host "Then run:  dotenvx run -- python main.py"
Write-Host "Docs: docs/dotenvx.md"
