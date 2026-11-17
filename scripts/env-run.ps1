# Run the bot with dotenvx (Bitwarden / .env.keys / DOTENV_PRIVATE_KEY).
# Usage: .\scripts\env-run.ps1 [args...]
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $Root

if (-not (Get-Command dotenvx -ErrorAction SilentlyContinue)) {
    Write-Error "dotenvx not found. Install: winget install dotenvx.dotenvx"
    exit 1
}

& dotenvx run -- python main.py @args
exit $LASTEXITCODE
