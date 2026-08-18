# Start the NetOps agent (FastAPI backend + built React frontend).
#
#   .\run.ps1              real MCPs
#   .\run.ps1 -Mock        mock CMDB / devices / Tufin / local probes
#   .\run.ps1 -Port 8001   if 8000 is taken
#   .\run.ps1 -Dev         also start the Vite dev server (hot reload, :5173)
param(
    [switch]$Mock,
    [switch]$Dev,
    [int]$Port = 8000
)

$here = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $here

if ($Mock) { $env:USE_MOCKS = "1" } else { $env:USE_MOCKS = "0" }

# build the frontend once if it has never been built
if (-not (Test-Path "$here\frontend\dist\index.html")) {
    Write-Host "frontend/dist missing - building it (one-off)..." -ForegroundColor Yellow
    Push-Location "$here\frontend"
    npm install
    npm run build
    Pop-Location
}

if ($Dev) {
    Start-Process powershell -ArgumentList "-NoExit", "-Command",
        "cd '$here\frontend'; npm run dev"
    Write-Host "Vite dev server starting on http://localhost:5173 (proxies to :$Port)"
}

Write-Host "Backend on http://localhost:$Port  (mocks: $env:USE_MOCKS)"
& "$here\.venv\Scripts\python.exe" -m uvicorn api.main:app --host 127.0.0.1 --port $Port
