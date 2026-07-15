#Requires -Version 5.1
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location -LiteralPath $Root

if (-not (Test-Path ".\.venv\Scripts\python.exe")) {
  Write-Host "[*] Creating venv..." -ForegroundColor Cyan
  python -m venv .venv
  & .\.venv\Scripts\python.exe -m pip install -U pip
  & .\.venv\Scripts\python.exe -m pip install -r requirements.txt
}

if (-not (Test-Path ".\config.json")) {
  Copy-Item ".\config.example.json" ".\config.json"
  Write-Host "[*] Created config.json" -ForegroundColor Yellow
}

$cfg = Get-Content ".\config.json" -Raw | ConvertFrom-Json
$host_ = if ($cfg.host) { $cfg.host } else { "0.0.0.0" }
$port = if ($cfg.port) { $cfg.port } else { 8787 }

Write-Host "[*] Starting Grok CLI Proxy on http://$host_`:$port" -ForegroundColor Green
Write-Host "[*] UI: http://127.0.0.1:$port/" -ForegroundColor Green
Write-Host "[*] API key is in config.json" -ForegroundColor Yellow

& .\.venv\Scripts\python.exe -m uvicorn app.main:app --host $host_ --port $port
